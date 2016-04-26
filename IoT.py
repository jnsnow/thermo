#!/usr/bin/python

# Can enable debug output by uncommenting:
#import logging
#logging.basicConfig(level=logging.DEBUG)

import time
import datetime
import Adafruit_MCP9808.MCP9808 as MCP9808
import RPi.GPIO as GPIO
import pprint
import time
from threading import Timer
import signal

# Relay settings
# Port 1: Green Wire. GPIO 21
# Port 2: Yellow Wire. GPIO 20
# Port 3: White Wire. GPIO 16
# (Red wire goes to all three ports.)

#def pircb(pin):
#    print "%s motion detected - pin %d" % (str(datetime.datetime.now()), pin)

#def miccb(pin):
#    print "%s sound detected (40dB VPP) - pin %d" % (str(datetime.datetime.now()), pin)

def c_to_f(c):
	return c * 9.0 / 5.0 + 32.0

def f_to_c(f):
        return (float(f) - 32.0) / 1.8

def minutes():
        t = datetime.datetime.now()
        return t.hour * 60 + t.minute

class TempConfig(object):
    _temp = 70.0
    _fan = "auto"
    _heat = "auto"
    _cooling = "auto"
    def __init__(self, temp=70.0, fan=False, heat=True, cooling=True):
        self._temp = temp
        self._fan = "on" if fan else "auto"
        self._heat = "auto" if heat else "off"
        self.cooling = "auto" if cooling else "off"
    def __str__(self):
        return self.get()
    def get(self):
        return { "temp": self._temp,
                 "fan": self._fan,
                 "heat": self._heat,
                 "cooling": self._cooling }
    def fanIsAuto(self):
        return self._fan == "auto"
    def fanIsOn(self):
        return self._fan == "on"
    def heatEnabled(self):
        return self._heat == "auto"
    def coolEnabled(self):
        return self._cooling == "auto"
    def temp(self):
        return self._temp


class ScheduleTempConfig(TempConfig):
    _override = None
    def __init__(self, override=None, *args, **kwargs):
        self._override = override
        super(self.__class__,self).__init__(*args, **kwargs)
    def __str__(self):
        return self.get()
    def get(self):
        tmp = super(self.__class__,self).get()
        tmp['override'] = self._override
        return tmp


class Sensor:
    _pin = -1
    def __init__(self, pin):
        self._pin = pin
        GPIO.setup(pin, GPIO.IN)
        #GPIO.add_event_detect(pin, GPIO.RISING)
        #GPIO.add_event_callback(pin, pircb)
    def state(self):
        return GPIO.input(self._pin)


class Thermostat:
    # Sensor Objects
    _sensor = None
    _pir = None
    _mic = None

    # Cache/State:
    _status = None
    _temp = 0.0
    _mode = "schedule"  # Operating mode: schedule/manual/override
    _activity = None    # Points to which activity override is active, if any
    _scheduled = None   # Currently scheduled program
    _next = 0           # Time to next schedule
    _statdata = None    # Statistics for the last window
    _statistics = None  # Statistics for the current window

    # Settings
    _settings = { "electric": True,
                  "ac": True,
                  "window": 15,
                  "hysteresis": 1.0 }  # +- 1.0C before we attempt to correct the temperature (roughly 2F)

    # 'Read Only' settings
    _pins = { "pir":     18,
              "mic":     23,
              "ctl": { "fan":     21,
                       "cooling": 20,
                       "heat":    16,
                       "demo":    12  } }

    ### Schedule and Programming Data ###

    # Generic default weekday schedule (No PID)
    _weekday = [(0, ScheduleTempConfig(temp=f_to_c(60.0))),    #12:00AM: 60F
                (360, ScheduleTempConfig(temp=f_to_c(65.0))),  #6:00AM: 65F
                (420, ScheduleTempConfig(temp=f_to_c(70.0))),  #7:00AM: 70F
                (600, ScheduleTempConfig(temp=f_to_c(60.0))),  #10:00AM: 60F
                (960, ScheduleTempConfig(temp=f_to_c(65.0))),  #4:00PM: 65F
                (1020, ScheduleTempConfig(temp=f_to_c(70.0))), #5:00PM: 70F
                (1380, ScheduleTempConfig(temp=f_to_c(60.0)))] #11:00PM: 60F

    # Generic default weekend schedule
    _weekend = [(0, ScheduleTempConfig(temp=f_to_c(60.0))),    #12:00AM: 60F
                (480, ScheduleTempConfig(temp=f_to_c(65.0))),  #8:00AM: 65F
                (540, ScheduleTempConfig(temp=f_to_c(70.0))),  #9:00AM: 70F
                (1380, ScheduleTempConfig(temp=f_to_c(60.0)))] #11:00PM: 60F

    # Default schedule: Weekdays and Weekends.
    _schedule = {
        0: _weekday,
        1: _weekday,
        2: _weekday,
        3: _weekday,
        4: _weekday,
        5: _weekend,
        6: _weekend
    }

    # Manual override settings
    _manual = TempConfig(temp=70.0, fan=False, heat=True, cooling=True)
    
    # Activity override settings
    _overrides = {
        "default": TempConfig(temp=70.0, fan=False, heat=True, cooling=True)
    }


    ## Initialization ##    
    
    def __init__(self, electric=True, ac=False):
        GPIO.setmode(GPIO.BCM)
        for k,v in self._pins['ctl'].items():
            GPIO.setup(v, GPIO.OUT, initial=GPIO.LOW)
        self._sensor = MCP9808.MCP9808()
        self._sensor.begin()
        self._pir = Sensor(self._pins['pir'])
        self._mic = Sensor(self._pins['mic'])
        self._settings['electric'] = electric
        self._settings['ac'] = ac
        self.schedule_change()
        self.status()
        # If we use gas heat and have no A/C, nothing tries to engage the fan.
        # Force it on if so-configured.
        self._fan(False)

        
    ## Raw Thermostat control methods ##
    
    def _heatpin(self, high=True):
        if (self.heatState() != high):
            print "HEAT: Transition from %s to %s" % (self.heatState(), high)
        GPIO.output(self._pins['ctl']['heat'],
                    GPIO.HIGH if high else GPIO.LOW)
        self._demopin(high)

    def _demopin(self, high=True):
        GPIO.output(self._pins['ctl']['demo'],
                    GPIO.HIGH if high else GPIO.LOW)

    def _coolpin(self, high=True):
        if (self.coolState() != high):
            print "COOL: Transition from %s to %s" % (self.coolState(), high)
        GPIO.output(self._pins['ctl']['cooling'],
                    GPIO.HIGH if high else GPIO.LOW)

    def _fanpin(self, high=True):
        if (self.fanState() != high):
            print "FAN: Transition from %s to %s" % (self.fanState(), high)
        GPIO.output(self._pins['ctl']['fan'],
                    GPIO.HIGH if high else GPIO.LOW)

        
    ## Smart Thermostat control methods ##
    
    def _heat(self, engage=True):
        self._heatpin(engage)
        # Electric utilizes the fan, Gas allows furnace to control fan to avoid cold air.
        if (self._settings['electric']):
            self._fan(engage)

    def _cool(self, engage=True):
        self._coolpin(engage)
        # A/C mode engages the fan, "cooling" does not.
        if (self._settings['ac']):
            self._fanpin(engage)

    def _fan(self, engage=True):
        # Disallow disengagement if the fan is full-on.
        if self.program().fanIsOn():
            self._fanpin(True)
        else:
            self._fanpin(engage)


    ## Sensor read methods ##

    def readTempC(self):
        self._temp = self._sensor.readTempC()
        return self._temp

    def pirState(self):
        return bool(self._pir.state())

    def micState(self):
        return bool(self._mic.state())

    def fanState(self):
        return bool(GPIO.input(self._pins['ctl']['fan']))

    def heatState(self):
        return bool(GPIO.input(self._pins['ctl']['heat']))

    def coolState(self):
        return bool(GPIO.input(self._pins['ctl']['cooling']))


    # Schedule / Mode Management

    # Determine, cache, and return the presently scheduled program,
    # ignoring the current mode of the device.
    # Bug: If the first schedule of the day does not start at 0 minutes,
    # It will be treated as if it was anyway.
    def scheduled_program(self):
        dailyprogram = self._schedule[datetime.datetime.now().weekday()]
        m = minutes()
        oldprogram = dailyprogram[0]
        for (time, program) in dailyprogram:
            if (m < time):
                self._scheduled = oldprogram
                self._next = time - m
                return self._scheduled
            oldprogram = program
        # Next schedule change is tomorrow. Current schedule is the last today.
        self._scheduled = oldprogram
        self._next = 1440 - m
        return self._scheduled

    # Called to install the currently scheduled program as the active schedule slice.
    # Does not change the mode of the device (i.e. from manual/override to schedule.)
    def schedule_change(self):
        self.scheduled_program()
        print "schedule_change invoked, current program: %s" % str(self._scheduled.get())
        print "Next schedule change in %d minutes" % self._next
        self._timer = Timer(self._next * 60, self.schedule_change, ())
        self._timer.start()

    # Returns the current operating program settings.
    def program(self):
        if (self._mode == "manual"):
            return self._manual
        elif (self._mode == "override"):
            if self._activity in self._overrides:
                return self._overrides[self._activity]
        elif (self._mode == "schedule"):
            return self._scheduled
        # Mode is incorrect or override has a bad name
        return self._manual
    


    ## Dragons ##



    # Updates and returns the current status of the device.
    def status(self):
        self._status = {
            "temp":     self.readTempC(),
            "heat":     self.heatState(),
            "cooling":  self.coolState(),
            "fan":      self.fanState(),
            "mic":      self.micState(),
            "pir":      self.pirState(),
            "time":     str(datetime.datetime.now()),
            "mode":     self._mode,
            "activity": self._activity
        }
        return self._status

    # Return the current list of override settings
    def overrides(self):
        tmp = {}
        for k,d in self._overrides.items():
            tmp[k] = d.get()
        return tmp

    # Returns the entire schedule.
    def schedule(self):
        return self._schedule

    # Returns giant JSON blob representing all relevant state
    def state(self):
        self._state = {
            "status": self.status(),
            "program": {
                "schedule": self.schedule(),
                "manual": self._manual.get(),
                "overrides": self.overrides()
            },
            "data": self._statdata
        }
        return self._state

    def _newAvg(self, key, new):
        n = self._statistics['nsamples']
        avg = ((self._statistics[key] * n) + new) / (n + 1)
        self._statistics[key] = avg
        return avg

    def _updateStats(self):
        if (self._statistics == None):
            self._statistics = { "start": datetime.datetime.now(),
                                 "nsamples": 1,
                                 "pir": float(self.pirState()),
                                 "mic": float(self.micState()),
                                 "fan": float(self.fanState()),
                                 "heat": float(self.heatState()),
                                 "cooling": float(self.coolState()),
                                 "temp": self.readTempC() }
            return False

        self._newAvg('pir', float(self.pirState()))
        self._newAvg('mic', float(self.micState()))
        self._newAvg('fan', float(self.fanState()))
        self._newAvg('heat', float(self.heatState()))
        self._newAvg('cooling', float(self.coolState()))
        self._newAvg('temp', self.readTempC())
        self._statistics['nsamples'] += 1
        now = datetime.datetime.now()
        tdx = now - self._statistics['start']
        if (tdx.total_seconds() >= (self._settings['window'] * 60)):
            self._statdata = self._statistics
            self._statdata['end'] = str(now)
            self._statistics = None
            return True
        return False


    _pp = pprint.PrettyPrinter(indent=4)
    def tick(self):
        prog = self.program()
        ttu = self._updateStats()

        # Cool
        if (self._temp > (prog.temp() + self._settings['hysteresis'])) and prog.coolEnabled():
            self._cool(True)
        elif (self._temp < (prog.temp() - self._settings['hysteresis'])):
            self._cool(False)

        # Heat
        if (self._temp < (prog.temp() - self._settings['hysteresis'])) and prog.heatEnabled():
            self._heat(True)
        elif (self._temp > (prog.temp() + self._settings['hysteresis'])):
            self._heat(False)

        # TODO: Stat window has expired, push status to cloud
        if ttu:
           print "%s" % self.state()
                                              
        
if __name__ == "__main__":
    thermo = Thermostat()
    print '%s' % str(thermo.overrides())
    thermo._pp.pprint(thermo.state())
    
    print 'Press Ctrl-C to quit.'
    i = 0
    while True:
        thermo.tick()
	time.sleep(1.0)
