#!/usr/bin/python
# Copyright (c) 2014 Adafruit Industries
# Author: Tony DiCola
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

# Can enable debug output by uncommenting:
#import logging
#logging.basicConfig(level=logging.DEBUG)

import time
import datetime
import Adafruit_MCP9808.MCP9808 as MCP9808
import RPi.GPIO as GPIO
import pprint

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
        return float(f) - 32.0 / 1.8

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
    def get(self):
        return { "temp": self._temp,
                 "fan": self._fan,
                 "heat": self._heat,
                 "cooling": self._cooling }


class ScheduleTempConfig(TempConfig):
    _override = None
    def __init__(self, override=None, *args, **kwargs):
        self._override = override
        super(self.__class__,self).__init__(*args, **kwargs)
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

    # Read-Only Cache:
    _status = None
    _temp = 0.0
    _mode = "schedule"
    _activity = None    # Points to which activity override is active, if any
    
    # Settings
    _electric = True
    _ac = False
    _stats_window = 15
    _pins = { "pir":     18,
              "mic":     23,
              "ctl": { "fan":     21,
                       "cooling": 20,
                       "heat":    16,
                       "demo":    12  } }

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
    # Statistical data
    _statdata = None

    # Internal State
    _statistics = None

    def __init__(self, electric=True):
        GPIO.setmode(GPIO.BCM)
        for k,v in self._pins['ctl'].items():
            GPIO.setup(v, GPIO.OUT, initial=GPIO.LOW)
        self._sensor = MCP9808.MCP9808()
        self._sensor.begin()
        self._pir = Sensor(self._pins['pir'])
        self._mic = Sensor(self._pins['mic'])
        self._electric = electric
        self.status()

    def _heatpin(self, high=True):
        GPIO.output(self._pins['ctl']['heat'],
                    GPIO.HIGH if high else GPIO.LOW)

    def _demopin(self, high=True):
        GPIO.output(self._pins['ctl']['demo'],
                    GPIO.HIGH if high else GPIO.LOW)

    def _coolpin(self, high=True):
        GPIO.output(self._pins['ctl']['cooling'],
                    GPIO.HIGH if high else GPIO.LOW)

    def _fanpin(self, high=True):
        # TODO: If current config has "fan == on", refuse to disengage pin
        GPIO.output(self._pins['ctl']['fan'],
                    GPIO.HIGH if high else GPIO.LOW)

    def _heat(self, engage=True):
        self._heatpin(engage)
        if (self._electric):
            self._fanpin(engage)

    def _cool(self, engage=True):
        self._coolpin(engage)
        # A/C mode engages the fan, "cooling" does not.
        if (self._ac):
            self._fanpin(engage)

    def _fan(self, engage=True):        
        pass
        # TODO: consider the modes and engage the fan accordingly

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

    def schedule(self):
        return self._schedule

    # Returns the current operating program settings.
    def program(self):
        if (self._mode == "manual"):
            return self._manual
        elif (self._mode == "override"):
            if self._activity in self._overrides:
                return self._overrides[self._activity]
        elif (self._mode == "schedule"):
            return None #TODO FIXME

        # Mode is incorrect or override has a bad pointer
        return self._manual

    
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

    _pp = pprint.PrettyPrinter(indent=4)
    def tick(self):
        if (self._statistics == None):
            self._statistics = { "start": datetime.datetime.now(),
                                 "nsamples": 1,
                                 "pir": float(self.pirState()),
                                 "mic": float(self.micState()),
                                 "fan": float(self.fanState()),
                                 "heat": float(self.heatState()),
                                 "cooling": float(self.coolState()),
                                 "temp": self.readTempC() }
        else:
            self._newAvg('pir', float(self.pirState()))
            self._newAvg('mic', float(self.micState()))
            self._newAvg('fan', float(self.fanState()))
            self._newAvg('heat', float(self.heatState()))
            self._newAvg('cooling', float(self.coolState()))
            self._newAvg('temp', self.readTempC())
            self._statistics['nsamples'] += 1
            now = datetime.datetime.now()
            tdx = now - self._statistics['start']
            if (tdx.total_seconds() > (self._stats_window * 60)):
                self._statdata = self._statistics
                self._statdata['end'] = str(now)
                self._statistics = None
                # TODO: PUSH JSON UPSTREAM HERE
                print "%s" % self.state()
        pass
                                              
        
if __name__ == "__main__":
    thermo = Thermostat()
    cfg = TempConfig()
    print '%s' % str(cfg.get())
    cfg2 = ScheduleTempConfig()
    print '%s' % str(cfg2.get())
    print '%s' % str(thermo.overrides())
    thermo._pp.pprint(thermo.state())
    print 'Press Ctrl-C to quit.'
    i = 0
    while True:
        thermo.tick()
	time.sleep(1.0)
        if (i <= 5):
            thermo._demopin(True)
	elif (i >= 5 and i <= 10):
            thermo._demopin(False)
        else:
            i = 0
        i += 1
