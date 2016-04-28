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
import paho.mqtt.client as mqtt
import ssl
import json

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

def STC(t, *args, **kwargs):
    return ScheduleTempConfig(*args, temp=f_to_c(t), override="default", **kwargs)


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
    # mqtt state
    _mqttc = None
    
    # Sensor Objects
    _sensor = None
    _pir = None
    _mic = None

    # Cache/State: Generally not communicated to the cloud
    _status = None      # Cache for device status 
    _temp = 0.0         # Last-read temperature
    _mode = "schedule"  # Operating mode: schedule/activity/manual
    _activity = None    # Points to which activity override is active, if any
    _scheduled = None   # Currently scheduled program
    _next = 0           # Time to next schedule
    _statdata = None    # Statistics for the last window
    _statistics = None  # Statistics for the current window
    _timer = None       # Timer obj for schedule change.
    _expiryTime = None  # Time at which activity mode, if enabled, will expire
    _resumeTime = None  # Time at which override mode, if enabled, will expire
    _lastAct = False    # Previous activity state

    # Settings
    _settings = { "electric": True,         # Electric, or gas/oil?
                  "ac": False,              # Air conditioning, or fan only?
                  "window": 15,             # Statistics sampling window
                  "hysteresis": 1.0,        # +- 1.0C before we attempt to correct the temperature (roughly 2F)
                  "tickprint": False,       # Debug: Print ticks?
                  "override_duration": 120, # Manual override duration, minimum
                  "sensors": True,          # Allow activity-based overrides at all?
                  "partial": True,          # Allow partial sensor activity, or all-or-nothing?
                  "partial_duration": 5,    # Partial activity override remains active for at least this long
                  "activity_duration": 15   # Activity override remains active for at least this long
                }


    # Command verb for AWS IoT
    _command = None

    # 'Offline / Read Only' settings
    _pins = { "pir":     18,
              "mic":     23,
              "ctl": { "fan":     21,
                       "cooling": 20,
                       "heat":    16,
                       "demo":    12  } }

    ### Schedule and Programming Data ###

    # Generic default weekday schedule (No PID)
    _weekday = [(0,    STC(60)), #12:00AM: 60F
                (360,  STC(65)), #6:00AM: 65F
                (420,  STC(70)), #7:00AM: 70F
                (600,  STC(60)), #10:00AM: 60F
                (960,  STC(65)), #4:00PM: 65F
                (1020, STC(70)), #5:00PM: 70F
                (1380, STC(60))  #11:00PM: 60F
               ]

    # Generic default weekend schedule
    _weekend = [(0,    STC(60)), #12:00AM: 60F
                (480,  STC(65)), #08:00AM: 65F
                (540,  STC(70)), #09:00AM: 70F
                (1380, STC(60))  #11:00PM: 60F
               ]

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
    _manual = TempConfig(temp=21.11, fan=False, heat=True, cooling=True)
    
    # Activity override settings
    _overrides = {
        "default": TempConfig(temp=21.667, fan=False, heat=True, cooling=True)
    }

    ## Initialization ##    
    
    def __init__(self, electric=True, ac=False):
        GPIO.setmode(GPIO.BCM)
        for k,v in self._pins['ctl'].items():
            GPIO.setup(v, GPIO.OUT, initial=GPIO.LOW)

        self._mqtt_init()
            
        self._sensor = MCP9808.MCP9808()
        self._sensor.begin()
        self._pir = Sensor(self._pins['pir'])
        self._mic = Sensor(self._pins['mic'])
        self._settings['electric'] = electric
        self._settings['ac'] = ac
        # Before we start the schedule, install a handler to kill any async timers.
        self._basesig = signal.signal(signal.SIGINT, self._sig)
        self.schedule_change()
        self.status()
        # If we use gas heat and have no A/C, nothing tries to engage the fan.
        # Force it on if so-configured.
        self._fan(False)

    ## MQTT (AWS IoT) methods ##

    def _mqtt_init(self):
        m = mqtt.Client(client_id="rpithermo")
        m.on_connect = self._mqtt_connect
        m.on_subscribe = self._mqtt_subscribe
        m.on_message = self._mqtt_message
        m.tls_set(ca_certs="/etc/ssl/certs/ca-certificates.crt",
                  certfile="/home/pi/.keys/da90ba97b5-certificate.pem.crt",
                  keyfile="/home/pi/.keys/da90ba97b5-private.pem.key",
                  tls_version=ssl.PROTOCOL_TLSv1_2)
        rsp = m.connect("A3S46MRJUBJIPY.iot.us-east-1.amazonaws.com", port=8883)
        if rsp != 0:
            self._log("MQTT connect failed.")
        m.loop_start()
        self._mqttc = m
                            

    def _mqtt_connect(self, mqtcc, obj, flags, rc):
        self._log("Subscriber Connection status code: %d; Connection status: %s" % (
                  rc, "Successful" if rc == 0 else "Refused"))
        if rc == 0:
            (_, m) = mqtcc.subscribe("$aws/things/thermo/shadow/update/delta", qos=1)
            (_, m) = mqtcc.subscribe("$aws/things/thermo/shadow/get/accepted", qos=1)
            self._getmid = m

    def _mqtt_subscribe(self, mqttc, obj, mid, granted_qos):
        self._log("Subscribed: mid: '%s'; qos: '%s'; data: '%s'" % (
                  str(mid),
                  str(granted_qos),
                  str(obj)))
        if mid == self._getmid:
            self._mqtt_retrieve()

    def _mqtt_message(self, mqtt, obj, msg):
        self._log("Received message from topic '%s', QoS '%d'" % (
                str(msg.topic),
                msg.qos))
        if msg.topic == "$aws/things/thermo/shadow/update/delta":
            dobj = json.loads(msg.payload)
            if 'state' not in dobj:
                self._log("MQTT status update did not include 'state' section, ignoring")
                return
            self._aws_update(dobj['state'])
        if msg.topic == "$aws/things/thermo/shadow/get/accepted":
            if not self._askShadow:
                # "I didn't ask for this."
                return
            dobj = json.loads(msg.payload)
            if 'state' not in dobj:
                self._log("MQTT reply did not include 'state' section, ignoring")
                return
            dobj = dobj['state']
            if "reported" in dobj:
                self._aws_update(dobj['reported'])
            else:
                self._log("Reported section absent from AWS.")
            if "delta" in dobj:
                self._aws_update(dobj['delta'])
            self._askShadow = False
        self._mqtt_publish()

    def _mqtt_publish(self):
        self._log("Publishing to AWS IoT")
        self._mqttc.publish("$aws/things/thermo/shadow/update",
                            json.dumps(self.state()),
                            qos=1)

    def _mqtt_publish_stats(self):
        self._log("Publishing statistics to AWS IoT")
        self._mqttc.publish("$aws/things/thermo/shadow/update",
                            json.dumps({"state": {"reported": {"data": self._statdata}}}),
                            qos=1)

    def _mqtt_retrieve(self):
        self._askShadow = True
        self._log("Asking AWS IoT for Shadow")
        self._mqttc.publish("$aws/things/thermo/shadow/get",
                            json.dumps({}),
                            qos=1)

    ## Cloud Update Methods ##

    def _aws_update(self, desired):
        for (k,v) in desired.items():
            if k == "settings":
                self._aws_settings(v)
            elif k == "program":
                self._log("Unimplemented: PROGRAM update")
            elif k == "command":
                self._aws_command(v)
            elif k == "status":
                self._log("Ignoring read-only update to 'status' from MQTT")
            elif k == "data":
                self._log("Ignoring read-only update to 'data' from MQTT")
            else:
                self._log("Ignoring update to unknown field '%s' from MQTT" % k)
        self._log("Finished updating state from MQTT")

    def _aws_settings(self, settings):
        for (k,v) in settings.items():
            if k == "electric":
                self._settings[k] = bool(v)
            elif k == "ac":
                self._settings[k] = bool(v)
            elif k == "window":
                self._settings[k] = int(v)
            elif k == "hysteresis":
                self._settings[k] = float(v)
            elif k == "tickprint":
                self._settings[k] = bool(v)
            elif k == "override_duration":
                self._settings[k] = int(v)
            elif k == "sensors":
                self._settings[k] = bool(v)
            elif k == "partial":
                self._settings[k] = bool(v)
            elif k == "partial_duration":
                self._settings[k] = int(v)
            elif k == "activity_duration":
                self._settings[k] = int(v)
            else:
                self._log("Ignoring unknown setting %s=%s" % (k, v))
        self._log("Updated settings")

    def _aws_command(self, cmd):
        if cmd == "refresh":
            self.refresh()
        elif cmd == "manual":
            self._engageManual()
        elif cmd == "resume":
            self._expireManual()
        else:
            self._log("Ignoring unknown command '%s'" % cmd)

    ## Raw Thermostat control methods ##
    
    def _heatpin(self, high=True):
        if (self.heatState() != high):
            self._log("HEAT: Transition from %s to %s" % (self.heatState(), high))
        GPIO.output(self._pins['ctl']['heat'],
                    GPIO.HIGH if high else GPIO.LOW)
        self._demopin(high)

    def _demopin(self, high=True):
        GPIO.output(self._pins['ctl']['demo'],
                    GPIO.HIGH if high else GPIO.LOW)

    def _coolpin(self, high=True):
        if (self.coolState() != high):
            self._log("COOL: Transition from %s to %s" % (self.coolState(), high))
        GPIO.output(self._pins['ctl']['cooling'],
                    GPIO.HIGH if high else GPIO.LOW)

    def _fanpin(self, high=True):
        if (self.fanState() != high):
            self._log("FAN: Transition from %s to %s" % (self.fanState(), high))
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
        self._log("schedule_change invoked, currently scheduled program: %s" % str(self._scheduled.get()))
        self._log("Next schedule change in %d minutes" % self._next)
        self._timer = Timer(self._next * 60, self.schedule_change, ())
        self._timer.setDaemon(True)
        self._timer.start()

    # Returns the current operating program settings.
    def program(self):
        if (self._mode == "manual"):
            return self._manual
        elif (self._mode == "activity"):
            if self._activity in self._overrides:
                return self._overrides[self._activity]
        # Mode is "schedule", something unrecognized,
        # or the override name is incorrect.
        return self._scheduled

    def refresh(self):
        self._log("Refreshing schedule")
        if (self._timer):
            self._timer.cancel()
        self.schedule_change()
        # Note: any changes to manual or overrides will be picked up by self.program() automatically.

    # Clean up any timers on exit.
    def _sig(self, signum, frame):
        if self._timer:
            self._timer.cancel()
        self._basesig(signum, frame)
    

    ## Serialization and Deserialization methods for AWS IoT ##

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
        return {d: [(ss[0], ss[1].get()) for ss in ds] for (d,ds) in self._schedule.items()}

    # Returns giant JSON blob representing all relevant state
    def state(self):
        self._state = {
            "status": self.status(),
            "program": {
                "schedule": self.schedule(),
                "manual": self._manual.get(),
                "overrides": self.overrides()
            },
            "settings": self._settings,
            "command": self._command
        }
        if self._statdata:
            self._state['data'] = self._statdata
        return {"state": {"reported": self._state, "desired": None }}


    ## Thermo Mainloop Functions
    

    def _log(self, msg):
        print "[%s] %s" % (str(datetime.datetime.now()), msg)
            
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
            self._statdata['start'] = str(self._statistics['start'])
            self._statdata['end'] = str(now)
            self._statistics = None
            return True
        return False

    def _activityClock(self, m):
        expiry = datetime.datetime.now() + datetime.timedelta(minutes=m)
        if ((self._expiryTime == None) or (expiry > self._expiryTime)):
            self._expiryTime = expiry

    def _expireActivity(self):
        if (self._mode == "activity"):
            self._mode = "schedule"
            self._expiryTime = None
            self._log("Exiting activity override mode, re-entering normal schedule; %s" %
                      self.program().get())
            self._activity = None

    def _activityMsg(self, msg):
        dt = self._expiryTime - datetime.datetime.now()
        self._log("%s. Expiry time is %s, in %.2f minutes" % (
                  str(msg),
                  str(self._expiryTime),
                  dt.total_seconds() / 60.0))

    def _checkManual(self):
        if self._mode == "manual":
            if self._resumeTime and self._tickTime > self._resumeTime:
                self._expireManual()
            elif not self._resumeTime:
                self._expireManual()

    def _checkActivity(self):
        # User has requested no activity overrides globally. Ignore.
        if not self._settings['sensors']:
            self._expireActivity()
            return

        # No override configured for the current schedule. Ignore.
        prog = self._scheduled.get()
        if not 'override' in prog:
            self._expireActivity()
            return
        
        m = self.micState()
        p = self.pirState()
        act = False

        if (m != p) and self._settings['partial']:
            self._activityClock(self._settings['partial_duration'])
            act = True
        elif (m and p):
            self._activityClock(self._settings['activity_duration'])
            act = True

        if (self._expiryTime):
            if (self._expiryTime < datetime.datetime.now()):
                self._expireActivity()
            elif (self._mode == "schedule"):
                self._activity = prog['override']
                self._mode = "activity"
                self._log("Entering activity override mode. Program: %s" % self.program().get())
            elif (act != self._lastAct):
                if not act:
                    self._activityMsg("Activity ceased")
                else:
                    self._log("Sensor activity resumed")

        self._lastAct = act

    def _engageManual(self):
        self._resumeTime = datetime.datetime.now() + datetime.timedelta(
                               minutes=self._settings['override_duration'])
        self._log("Entering manual override mode. Expiry is %s, in %.2f minutes." % (
                  str(self._resumeTime),
                  (self._resumeTime - datetime.datetime.now()).total_seconds() / 60.0))
        self._mode = "manual"
        
    # Called to retire the Manual override.
    def _expireManual(self):
        if self._expiryTime and self._expiryTime < datetime.datetime.now():
            self._log("Leaving manual override mode. Returning to activity override.")
            self._mode = "activity"
        else:
            self._expiryTime = None
            self._log("Leaving manual override mode. Returning to regular schedule.")
            self._mode = "schedule"
        self._resumeTime = None
        
    def _checkThermo(self):
        prog = self.program()
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

    _pp = pprint.PrettyPrinter(indent=4)
    def tick(self, sleep=1.0):
        self._tickTime = datetime.datetime.now()
        ttu = self._updateStats()
        self._checkManual()
        self._checkActivity()
        self._checkThermo()

        if self._settings['tickprint']:
            self._log(str(self.status()))

        if ttu:
            self._mqtt_publish_stats()

        time.sleep(sleep)



## 3, 2, 1: Showtime! ##



if __name__ == "__main__":
    thermo = Thermostat()
    while True:
        thermo.tick(1.0)
