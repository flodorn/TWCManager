#! /usr/bin/python3

################################################################################
# Code and TWC protocol reverse engineering by Chris Dragon.
#
# Additional logs and hints provided by Teslamotorsclub.com users:
#   TheNoOne, IanAmber, and twc.
# Thank you!
#
# For support and information, please read through this thread:
# https://teslamotorsclub.com/tmc/threads/new-wall-connector-load-sharing-protocol.72830
#
# Report bugs at https://github.com/cdragon/TWCManager/issues
#
# This software is released under the "Unlicense" model: http://unlicense.org
# This means source code and TWC protocol knowledge are released to the general
# public free for personal or commercial use. I hope the knowledge will be used
# to increase the use of green energy sources by controlling the time and power
# level of car charging.
#
# WARNING:
# Misuse of the protocol described in this software can direct a Tesla Wall
# Charger to supply more current to a car than the charger wiring was designed
# for. This will trip a circuit breaker or may start a fire in the unlikely
# event that the circuit breaker fails.
# This software was not written or designed with the benefit of information from
# Tesla and there is always a small possibility that some unforeseen aspect of
# its operation could damage a Tesla vehicle or a Tesla Wall Charger. All
# efforts have been made to avoid such damage and this software is in active use
# on the author's own vehicle and TWC.
#
# In short, USE THIS SOFTWARE AT YOUR OWN RISK.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# For more information, please visit http://unlicense.org

################################################################################
# What's TWCManager good for?
#
# This script (TWCManager) pretends to be a Tesla Wall Charger (TWC) set to
# master mode. When wired to the IN or OUT pins of real TWC units set to slave
# mode (rotary switch position F), TWCManager can tell them to limit car
# charging to any whole amp value between 5A and the max rating of the charger.
# Charging can also be stopped so the car goes to sleep.
#
# This level of control is useful for having TWCManager track the real-time
# availability of green energy sources and direct the slave TWCs to use near the
# exact amount of energy available. This saves energy compared to sending the
# green energy off to a battery for later car charging or off to the grid where
# some of it is lost in transmission.
#
# TWCManager can also be set up to only allow charging during certain hours,
# stop charging if a grid overload or "save power day" is detected, reduce
# charging on one TWC when a "more important" one is plugged in, or whatever
# else you might want to do.
#
# One thing TWCManager does not have direct access to is the battery charge
# percentage of each plugged-in car. There are hints on forums that some TWCs
# do report battery state, but we have yet to see a TWC send such a message.
# It's possible the feature exists in TWCs with newer firmware.
# This is unfortunate, but if you own a Tesla vehicle being charged, people have
# figured out how to get its charge state by contacting Tesla's servers using
# the same password you use in the Tesla phone app. Be very careful not to
# expose that password because it allows unlocking and starting the car.

################################################################################
# Overview of protocol TWCs use to load share
#
# A TWC set to slave mode (rotary switch position F) sends a linkready message
# every 10 seconds.
# The message contains a unique 4-byte id that identifies that particular slave
# as the sender of the message.
#
# A TWC set to master mode sees a linkready message. In response, it sends a
# heartbeat message containing the slave's 4-byte id as the intended recipient
# of the message.
# The master's 4-byte id is included as the sender of the message.
#
# Slave sees a heartbeat message from master directed to its unique 4-byte id
# and responds with its own heartbeat message containing the master's 4-byte id
# as the intended recipient of the message.
# The slave's 4-byte id is included as the sender of the message.
#
# Master sends a heartbeat to a slave around once per second and expects a
# response heartbeat from the slave.
# Slaves do not send heartbeats without seeing one from a master first. If
# heartbeats stop coming from master, slave resumes sending linkready every 10
# seconds.
# If slaves stop replying to heartbeats from master, master stops sending
# heartbeats after about 26 seconds.
#
# Heartbeat messages contain a data block used to negotiate the amount of power
# available to each slave and to the master.
# The first byte is a status indicating things like is TWC plugged in, does it
# want power, is there an error, etc.
# Next two bytes indicate the amount of power requested or the amount allowed in
# 0.01 amp increments.
# Next two bytes indicate the amount of power being used to charge the car, also in
# 0.01 amp increments.
# Remaining bytes always contain a value of 0.

import serial
import time
import re
import subprocess
import random
import math
import struct
import sys
import traceback
import queue
import sysv_ipc
import json
from datetime import datetime
import threading
import paho.mqtt.client as mqtt

##########################
#
# Configuration parameters
#
# Most users will have only one ttyUSB adapter plugged in and the default value
# of '/dev/ttyUSB0' below will work. If not, run 'dmesg |grep ttyUSB' on the
# command line to find your rs485 adapter and put its ttyUSB# value in the
# parameter below.
# If you're using a non-USB adapter like an RS485 shield, the value may need to
# be something like '/dev/serial0'.
rs485Adapter = '/dev/ttyUSB0'

# Set wiringMaxAmpsAllTWCs to the maximum number of amps your charger wiring
# can handle. I default this to a low 6A which should be safe with the minimum
# standard of wiring in the areas of the world that I'm aware of.
# Most U.S. chargers will be wired to handle at least 40A and sometimes 80A,
# whereas EU chargers will handle at most 32A (using 3 AC lines instead of 2 so
# the total power they deliver is similar).
# Setting wiringMaxAmpsAllTWCs too high will trip the circuit breaker on your
# charger at best or START A FIRE if the circuit breaker malfunctions.
# Keep in mind that circuit breakers are designed to handle only 80% of their
# max power rating continuously, so if your charger has a 50A circuit breaker,
# put 50 * 0.8 = 40 here.
# 40 amp breaker * 0.8 = 32 here.
# 30 amp breaker * 0.8 = 24 here.
# 100 amp breaker * 0.8 = 80 here.
# IF YOU'RE NOT SURE WHAT TO PUT HERE, ASK THE ELECTRICIAN WHO INSTALLED YOUR
# CHARGER.
wiringMaxAmpsAllTWCs = 32

# If all your chargers share a single circuit breaker, set wiringMaxAmpsPerTWC
# to the same value as wiringMaxAmpsAllTWCs.
# Rarely, each TWC will be wired to its own circuit breaker. If you're
# absolutely sure your chargers each have a separate breaker, put the value of
# that breaker * 0.8 here, and put the sum of all breakers * 0.8 as the value of
# wiringMaxAmpsAllTWCs.
# For example, if you have two TWCs each with a 50A breaker, set
# wiringMaxAmpsPerTWC = 50 * 0.8 = 40 and wiringMaxAmpsAllTWCs = 40 + 40 = 80.
wiringMaxAmpsPerTWC = 16

# https://teslamotorsclub.com/tmc/threads/model-s-gen2-charger-efficiency-testing.78740/#post-1844789
# says you're using 10.85% more power (91.75/82.77=1.1085) charging at 5A vs 40A,
# 2.48% more power at 10A vs 40A, and 1.9% more power at 20A vs 40A.  This is
# using a car with 2nd generation onboard AC/DC converter (VINs ending in 20000
# and higher).
# https://teslamotorsclub.com/tmc/threads/higher-amp-charging-is-more-efficient.24972/
# says that cars using a 1st generation charger may use up to 30% more power
# at 6A vs 40A!  However, the data refers to 120V 12A charging vs 240V 40A
# charging. 120V 12A is technically the same power as 240V 6A, but the car
# batteries need 400V DC to charge and a lot more power is wasted converting
# 120V AC to 400V DC than 240V AC to 400V DC.
#
# The main point is 6A charging wastes a lot of power, so we default to charging
# at a minimum of 12A by setting minAmpsPerTWC to 12. I picked 12A instead of 10A
# because there is a theory that multiples of 3A are most efficient, though I
# couldn't find any data showing that had been tested.
#
# Most EU chargers are connected to 230V, single-phase power which means 12A is
# about the same power as in US chargers. If you have three-phase power, you can
# lower minAmpsPerTWC to 6 and still be charging with more power than 12A on
# single-phase.  For example, 12A * 230V * 1 = 2760W for single-phase power, while
# 6A * 230V * 3 = 4140W for three-phase power. Consult an electrician if this
# doesn't make sense.
#
# https://forums.tesla.com/forum/forums/charging-lowest-amperage-purposely
# says another reason to charge at higher power is to preserve battery life.
# The best charge rate is the capacity of the battery pack / 2.  Home chargers
# can't reach that rate, so charging as fast as your wiring supports is best
# from that standpoint.  It's not clear how much damage charging at slower
# rates really does.
minAmpsPerTWC = 6

# After determining how much green energy is available for charging, we add
# greenEnergyAmpsOffset to the value. This is most often given a negative value
# equal to the average amount of power consumed by everything other than car
# charging. For example, if your house uses an average of 2.8A to power
# computers, lights, etc while you expect the car to be charging, set
# greenEnergyAmpsOffset = -2.8.
#
# If you have solar panels, look at your utility meter while your car charges.
# If it says you're using 0.67kW, that means you should set
# greenEnergyAmpsOffset = -0.67kW * 1000 / 240V = -2.79A assuming you're on the
# North American 240V grid. In other words, during car charging, you want your
# utility meter to show a value close to 0kW meaning no energy is being sent to
# or from the grid.
greenEnergyAmpsOffset = 0

# Choose how much debugging info to output.
# 0 is no output other than errors.
# 1 is just the most useful info.
# 2-8 add debugging info
# 9 includes raw RS-485 messages transmitted and received (2-3 per sec)
# 10 is all info.
# 11 is more than all info.  ;)
debugLevel = 11

# Choose whether to display milliseconds after time on each line of debug info.
displayMilliseconds = False

# Normally we fake being a TWC Master using fakeMaster = 1.
# Two other settings are available, but are only useful for debugging and
# experimenting:
#   Set fakeMaster = 0 to fake being a TWC Slave instead of Master.
#   Set fakeMaster = 2 to display received RS-485 messages but not send any
#                      unless you use the debugging web interface
#                      (index.php?debugTWC=1) to send messages.
fakeMaster = 1

# TWC's rs485 port runs at 9600 baud which has been verified with an
# oscilloscope. Don't change this unless something changes in future hardware.
baud = 9600

# All TWCs ship with a random two-byte TWCID. We default to using 0x7777 as our
# fake TWC ID. There is a 1 in 64535 chance that this ID will match each real
# TWC on the network, in which case you should pick a different random id below.
# This isn't really too important because even if this ID matches another TWC on
# the network, that TWC will pick its own new random ID as soon as it sees ours
# conflicts.
fakeTWCID = bytearray(b'\x77\x77')

# TWCs send a seemingly-random byte after their 2-byte TWC id in a number of
# messages. I call this byte their "Sign" for lack of a better term. The byte
# never changes unless the TWC is reset or power cycled. We use hard-coded
# values for now because I don't know if there are any rules to what values can
# be chosen. I picked 77 because it's easy to recognize when looking at logs.
# These shouldn't need to be changed.
masterSign = bytearray(b'\x77')
slaveSign = bytearray(b'\x77')

#
# End configuration parameters
#
##############################


##############################
#
# Begin functions
#


def transmit_mqtt(mqttChannel, mqttPayload):
    client = mqtt.Client(client_id="P1")
    client.connect("192.168.1.11", 1883, 60) #connect to broker
    #client.loop_start()
    infot = client.publish(mqttChannel, mqttPayload) 
    infot.wait_for_publish()
    client.disconnect()



def time_now():
    global displayMilliseconds
    return(datetime.now().strftime("%H:%M:%S" + (
        ".%f" if displayMilliseconds else "")))

def hex_str(s:str):
    return " ".join("{:02X}".format(ord(c)) for c in s)

def hex_str(ba:bytearray):
    return " ".join("{:02X}".format(c) for c in ba)

def run_process(cmd):
    result = None
    try:
        result = subprocess.check_output(cmd, shell=True)
    except subprocess.CalledProcessError:
        # We reach this point if the process returns a non-zero exit code.
        result = b''

    return result


def load_settings():
    global debugLevel, settingsFileName, nonScheduledAmpsMax, scheduledAmpsMax, \
           scheduledAmpsStartHour, scheduledAmpsEndHour, \
           scheduledAmpsDaysBitmap, hourResumeTrackGreenEnergy, kWhDelivered
           
    try:
        fh = open(settingsFileName, 'r')

        for line in fh:
            m = re.search(r'^\s*nonScheduledAmpsMax\s*=\s*([-0-9.]+)', line, re.MULTILINE)
            if(m):
                nonScheduledAmpsMax = int(m.group(1))
                if(debugLevel >= 10):
                    print("load_settings: nonScheduledAmpsMax set to " + str(nonScheduledAmpsMax))
                continue

            m = re.search(r'^\s*scheduledAmpsMax\s*=\s*([-0-9.]+)', line, re.MULTILINE)
            if(m):
                scheduledAmpsMax = int(m.group(1))
                if(debugLevel >= 10):
                    print("load_settings: scheduledAmpsMax set to " + str(scheduledAmpsMax))
                continue

            m = re.search(r'^\s*scheduledAmpsStartHour\s*=\s*([-0-9.]+)', line, re.MULTILINE)
            if(m):
                scheduledAmpsStartHour = float(m.group(1))
                if(debugLevel >= 10):
                    print("load_settings: scheduledAmpsStartHour set to " + str(scheduledAmpsStartHour))
                continue

            m = re.search(r'^\s*scheduledAmpsEndHour\s*=\s*([-0-9.]+)', line, re.MULTILINE)
            if(m):
                scheduledAmpsEndHour = float(m.group(1))
                if(debugLevel >= 10):
                    print("load_settings: scheduledAmpsEndHour set to " + str(scheduledAmpsEndHour))
                continue

            m = re.search(r'^\s*scheduledAmpsDaysBitmap\s*=\s*([-0-9.]+)', line, re.MULTILINE)
            if(m):
                scheduledAmpsDaysBitmap = int(m.group(1))
                if(debugLevel >= 10):
                    print("load_settings: scheduledAmpsDaysBitmap set to " + str(scheduledAmpsDaysBitmap))
                continue

            m = re.search(r'^\s*hourResumeTrackGreenEnergy\s*=\s*([-0-9.]+)', line, re.MULTILINE)
            if(m):
                hourResumeTrackGreenEnergy = float(m.group(1))
                if(debugLevel >= 10):
                    print("load_settings: hourResumeTrackGreenEnergy set to " + str(hourResumeTrackGreenEnergy))
                continue

            m = re.search(r'^\s*kWhDelivered\s*=\s*([-0-9.]+)', line, re.MULTILINE)
            if(m):
                kWhDelivered = float(m.group(1))
                if(debugLevel >= 10):
                    print("load_settings: kWhDelivered set to " + str(kWhDelivered))
                continue   

            print(time_now() + ": load_settings: Unknown setting " + line)

        fh.close()

    except FileNotFoundError:
        pass

def save_settings():
    global debugLevel, settingsFileName, nonScheduledAmpsMax, scheduledAmpsMax, \
           scheduledAmpsStartHour, scheduledAmpsEndHour, \
           scheduledAmpsDaysBitmap, hourResumeTrackGreenEnergy, kWhDelivered
           

    fh = open(settingsFileName, 'w')
    fh.write('nonScheduledAmpsMax=' + str(nonScheduledAmpsMax) +
            '\nscheduledAmpsMax=' + str(scheduledAmpsMax) +
            '\nscheduledAmpsStartHour=' + str(scheduledAmpsStartHour) +
            '\nscheduledAmpsEndHour=' + str(scheduledAmpsEndHour) +
            '\nscheduledAmpsDaysBitmap=' + str(scheduledAmpsDaysBitmap) +
            '\nhourResumeTrackGreenEnergy=' + str(hourResumeTrackGreenEnergy) +
            '\nkWhDelivered=' + str(kWhDelivered)
            )

    fh.close()

def trim_pad(s:bytearray, makeLen):
    # Trim or pad s with zeros so that it's makeLen length.
    while(len(s) < makeLen):
        s += b'\x00'

    if(len(s) > makeLen):
        s = s[0:makeLen]

    return s


def send_msg(msg):
    # Send msg on the RS485 network. We'll escape bytes with a special meaning,
    # add a CRC byte to the message end, and add a C0 byte to the start and end
    # to mark where it begins and ends.
    global ser, timeLastTx, fakeMaster, slaveTWCRoundRobin

    msg = bytearray(msg)
    checksum = 0
    for i in range(1, len(msg)):
        checksum += msg[i]

    msg.append(checksum & 0xFF)

    # Escaping special chars:
    # The protocol uses C0 to mark the start and end of the message.  If a C0
    # must appear within the message, it is 'escaped' by replacing it with
    # DB and DC bytes.
    # A DB byte in the message is escaped by replacing it with DB DD.
    #
    # User FuzzyLogic found that this method of escaping and marking the start
    # and end of messages is based on the SLIP protocol discussed here:
    #   https://en.wikipedia.org/wiki/Serial_Line_Internet_Protocol
    i = 0
    while(i < len(msg)):
        if(msg[i] == 0xc0):
            msg[i:i+1] = b'\xdb\xdc'
            i = i + 1
        elif(msg[i] == 0xdb):
            msg[i:i+1] = b'\xdb\xdd'
            i = i + 1
        i = i + 1

    msg = bytearray(b'\xc0' + msg + b'\xc0')

    if(debugLevel >= 9):
        print("Tx@" + time_now() + ": " + hex_str(msg))

    ser.write(msg)

    timeLastTx = time.time()

def unescape_msg(msg:bytearray, msgLen):
    # Given a message received on the RS485 network, remove leading and trailing
    # C0 byte, unescape special byte values, and verify its data matches the CRC
    # byte.
    msg = msg[0:msgLen]

    # See notes in send_msg() for the way certain bytes in messages are escaped.
    # We basically want to change db dc into c0 and db dd into db.
    # Only scan to one less than the length of the string to avoid running off
    # the end looking at i+1.
    i = 0
    while i < len(msg):
        if(msg[i] == 0xdb):
            if(msg[i+1] == 0xdc):
                # Replace characters at msg[i] and msg[i+1] with 0xc0,
                # shortening the string by one character. In Python, msg[x:y]
                # refers to a substring starting at x and ending immediately
                # before y. y - x is the length of the substring.
                msg[i:i+2] = [0xc0]
            elif(msg[i+1] == 0xdd):
                msg[i:i+2] = [0xdb]
            else:
                print(time_now(), "ERROR: Special character 0xDB in message is " \
                  "followed by invalid character 0x%02X.  " \
                  "Message may be corrupted." %
                  (msg[i+1]))

                # Replace the character with something even though it's probably
                # not the right thing.
                msg[i:i+2] = [0xdb]
        i = i+1

    # Remove leading and trailing C0 byte.
    msg = msg[1:len(msg)-1]
    return msg


def send_master_linkready1():
    if(debugLevel >= 1):
        print(time_now() + ": Send master linkready1")

    # When master is powered on or reset, it sends 5 to 7 copies of this
    # linkready1 message followed by 5 copies of linkready2 (I've never seen
    # more or less than 5 of linkready2).
    #
    # This linkready1 message advertises master's TWCID to other slaves on the
    # network.
    # If a slave happens to have the same id as master, it will pick a new
    # random TWCID. Other than that, slaves don't seem to respond to linkready1.

    # linkready1 and linkready2 are identical except FC E1 is replaced by FB E2
    # in bytes 2-3. Both messages will cause a slave to pick a new id if the
    # slave's id conflicts with master.
    # If a slave stops sending heartbeats for awhile, master may send a series
    # of linkready1 and linkready2 messages in seemingly random order, which
    # means they don't indicate any sort of startup state.

    # linkready1 is not sent again after boot/reset unless a slave sends its
    # linkready message.
    # At that point, linkready1 message may start sending every 1-5 seconds, or
    # it may not be sent at all.
    # Behaviors I've seen:
    #   Not sent at all as long as slave keeps responding to heartbeat messages
    #   right from the start.
    #   If slave stops responding, then re-appears, linkready1 gets sent
    #   frequently.

    # One other possible purpose of linkready1 and/or linkready2 is to trigger
    # an error condition if two TWCs on the network transmit those messages.
    # That means two TWCs have rotary switches setting them to master mode and
    # they will both flash their red LED 4 times with top green light on if that
    # happens.

    # Also note that linkready1 starts with FC E1 which is similar to the FC D1
    # message that masters send out every 4 hours when idle. Oddly, the FC D1
    # message contains all zeros instead of the master's id, so it seems
    # pointless.

    # I also don't understand the purpose of having both linkready1 and
    # linkready2 since only two or more linkready2 will provoke a response from
    # a slave regardless of whether linkready1 was sent previously. Firmware
    # trace shows that slaves do something somewhat complex when they receive
    # linkready1 but I haven't been curious enough to try to understand what
    # they're doing. Tests show neither linkready1 or 2 are necessary. Slaves
    # send slave linkready every 10 seconds whether or not they got master
    # linkready1/2 and if a master sees slave linkready, it will start sending
    # the slave master heartbeat once per second and the two are then connected.
    send_msg(bytearray(b'\xFC\xE1') + fakeTWCID + masterSign + bytearray(b'\x00\x00\x00\x00\x00\x00\x00\x00'))


def send_master_linkready2():
    if(debugLevel >= 1):
        print(time_now() + ": Send master linkready2")

    # This linkready2 message is also sent 5 times when master is booted/reset
    # and then not sent again if no other TWCs are heard from on the network.
    # If the master has ever seen a slave on the network, linkready2 is sent at
    # long intervals.
    # Slaves always ignore the first linkready2, but respond to the second
    # linkready2 around 0.2s later by sending five slave linkready messages.
    #
    # It may be that this linkready2 message that sends FB E2 and the master
    # heartbeat that sends fb e0 message are really the same, (same FB byte
    # which I think is message type) except the E0 version includes the TWC ID
    # of the slave the message is intended for whereas the E2 version has no
    # recipient TWC ID.
    #
    # Once a master starts sending heartbeat messages to a slave, it
    # no longer sends the global linkready2 message (or if it does,
    # they're quite rare so I haven't seen them).
    send_msg(bytearray(b'\xFB\xE2') + fakeTWCID + masterSign + bytearray(b'\x00\x00\x00\x00\x00\x00\x00\x00'))

def send_slave_linkready():
    # In the message below, \x1F\x40 (hex 0x1f40 or 8000 in base 10) refers to
    # this being a max 80.00Amp charger model.
    # EU chargers are 32A and send 0x0c80 (3200 in base 10).
    #
    # I accidentally changed \x1f\x40 to \x2e\x69 at one point, which makes the
    # master TWC immediately start blinking its red LED 6 times with top green
    # LED on. Manual says this means "The networked Wall Connectors have
    # different maximum current capabilities".
    msg = bytearray(b'\xFD\xE2') + fakeTWCID + slaveSign + bytearray(b'\x1F\x40\x00\x00\x00\x00\x00\x00')
    if(self.protocolVersion == 2):
        msg += bytearray(b'\x00\x00')

    send_msg(msg)

def master_id_conflict():
    # We're playing fake slave, and we got a message from a master with our TWCID.
    # By convention, as a slave we must change our TWCID because a master will not.
    fakeTWCID[0] = random.randint(0, 0xFF)
    fakeTWCID[1] = random.randint(0, 0xFF)

    # Real slaves change their sign during a conflict, so we do too.
    slaveSign[0] = random.randint(0, 0xFF)

    print(time_now() + ": Master's TWCID matches our fake slave's TWCID.  " \
        "Picked new random TWCID %02X%02X with sign %02X" % \
        (fakeTWCID[0], fakeTWCID[1], slaveSign[0]))

def new_slave(newSlaveID, maxAmps):
    global slaveTWCs, slaveTWCRoundRobin

    try:
        slaveTWC = slaveTWCs[newSlaveID]
        # We didn't get KeyError exception, so this slave is already in
        # slaveTWCs and we can simply return it.
        return slaveTWC
    except KeyError:
        pass

    slaveTWC = TWCSlave(newSlaveID, maxAmps)
    slaveTWCs[newSlaveID] = slaveTWC
    slaveTWCRoundRobin.append(slaveTWC)

    if(len(slaveTWCRoundRobin) > 3):
        print("WARNING: More than 3 slave TWCs seen on network.  " \
            "Dropping oldest: " + hex_str(slaveTWCRoundRobin[0].TWCID) + ".")
        delete_slave(slaveTWCRoundRobin[0].TWCID)

    return slaveTWC

def delete_slave(deleteSlaveID):
    global slaveTWCs, slaveTWCRoundRobin

    for i in range(0, len(slaveTWCRoundRobin)):
        if(slaveTWCRoundRobin[i].TWCID == deleteSlaveID):
            del slaveTWCRoundRobin[i]
            break
    try:
        del slaveTWCs[deleteSlaveID]
    except KeyError:
        pass

def total_amps_actual_all_twcs():
    global debugLevel, slaveTWCRoundRobin, wiringMaxAmpsAllTWCs

    totalAmps = 0
    for slaveTWC in slaveTWCRoundRobin:
        totalAmps += slaveTWC.reportedAmpsActual
    if(debugLevel >= 10):
        print("Total amps all slaves are using: " + str(totalAmps))
        
    return totalAmps

def num_cars_charging_now():
    global debugLevel, slaveTWCRoundRobin
         
    carsCharging = 0
    for slaveTWC in slaveTWCRoundRobin:
        if(slaveTWC.reportedAmpsActual >= 1.0):
                carsCharging += 1
    if(debugLevel >= 10):
        print("BUGFIX: Number of cars charging now: " + str(carsCharging))
    
    return carsCharging


def check_green_energy():
    global debugLevel, maxAmpsToDivideAmongSlaves, greenEnergyAmpsOffset, \
           minAmpsPerTWC, backgroundTasksLock

    # I check solar panel generation using an API exposed by The
    # Energy Detective (TED). It's a piece of hardware available
    # at http://www. theenergydetective.com
    # You may also be able to find a way to query a solar system
    # on the roof using an API provided by your solar installer.
    # Most of those systems only update the amount of power the
    # system is producing every 15 minutes at most, but that's
    # fine for tweaking your car charging.
    #
    # In the worst case, you could skip finding realtime green
    # energy data and simply direct the car to charge at certain
    # rates at certain times of day that typically have certain
    # levels of solar or wind generation. To do so, use the hour
    # and min variables as demonstrated just above this line:
    #   backgroundTasksQueue.put({'cmd':'')
    #
    # The curl command used below can be used to communicate
    # with almost any web API, even ones that require POST
    # values or authentication. The -s option prevents curl from
    # displaying download stats. -m 60 prevents the whole
    # operation from taking over 60 seconds.
    greenEnergyData = run_process('curl -s -m 60 "http://192.168.1.11:8080/rest/items/powerForCars/state"')

    # In case, greenEnergyData will contain something like this:
    #   MTU, Time, Power, Cost, Voltage
    #   Solar,11/11/2017 14:20:43,-2.957,-0.29,124.3
    # The only part we care about is -2.957 which is negative
    # kW currently being generated. When 0kW is generated, the
    # negative disappears so we make it optional in the regex
    # below.
    
    #m = re.search(b'^Solar,[^,]+,-?([^, ]+),', greenEnergyData, re.MULTILINE)
    #if(m):
    
    solarW = int(float(greenEnergyData))

    # Use backgroundTasksLock to prevent changing maxAmpsToDivideAmongSlaves
    # if the main thread is in the middle of examining and later using
    # that value.
    # Watts = Volts * Amps
    # Car charges at 240 volts in North America so we figure
    # out how many amps * 240 = solarW and limit the car to
    # that many amps.
    totalAmpsUsed = total_amps_actual_all_twcs()
    maxAmpsToDivideAmongSlaves = (solarW / 230 / 3) + totalAmpsUsed
 
    if(debugLevel >= 1):
        print("%s: Solar generating %dW so limit car charging to:\n" \
                 "          %.2fA + %.2fA = %.2fA.  Charge when above %.0fA (minAmpsPerTWC)." % \
                 (time_now(), solarW, (solarW / 230 / 3),
                 totalAmpsUsed, maxAmpsToDivideAmongSlaves,
                 minAmpsPerTWC))

#
# End functions
#
##############################



##############################
#
# Begin TWCSlave class
#

class TWCSlave:
    TWCID = None
    maxAmps = None

    # Protocol 2 TWCs tend to respond to commands sent using protocol 1, so
    # default to that till we know for sure we're talking to protocol 2.
    protocolVersion = 1
    minAmpsTWCSupports = 6
    masterHeartbeatData = bytearray(b'\x00\x00\x00\x00\x00\x00\x00\x00\x00')
    timeLastRx = time.time()

    # reported* vars below are reported to us in heartbeat messages from a Slave
    # TWC.
    reportedAmpsMax = 0
    reportedAmpsActual = 0
    reportedState = 0

    # reportedAmpsActual frequently changes by small amounts, like 5.14A may
    # frequently change to 5.23A and back.
    # reportedAmpsActualSignificantChangeMonitor is set to reportedAmpsActual
    # whenever reportedAmpsActual is at least 0.8A different than
    # reportedAmpsActualSignificantChangeMonitor. Whenever
    # reportedAmpsActualSignificantChangeMonitor is changed,
    # timeReportedAmpsActualChangedSignificantly is set to the time of the
    # change. The value of reportedAmpsActualSignificantChangeMonitor should not
    # be used for any other purpose. timeReportedAmpsActualChangedSignificantly
    # is used for things like preventing start and stop charge on a car more
    # than once per minute.
    reportedAmpsActualSignificantChangeMonitor = -1
    timeReportedAmpsActualChangedSignificantly = time.time()

    lastAmpsOffered = -1
    timeLastAmpsOfferedChanged = time.time()
    lastHeartbeatDebugOutput = ''
    timeLastHeartbeatDebugOutput = 0
    wiringMaxAmps = wiringMaxAmpsPerTWC

    def __init__(self, TWCID, maxAmps):
        self.TWCID = TWCID
        self.maxAmps = maxAmps

    def print_status(self, heartbeatData):
        global fakeMaster, masterTWCID

        try:
            debugOutput = ": SHB %02X%02X: %02X %05.2f/%05.2fA %02X%02X" % \
                (self.TWCID[0], self.TWCID[1], heartbeatData[0],
                (((heartbeatData[3] << 8) + heartbeatData[4]) / 100),
                (((heartbeatData[1] << 8) + heartbeatData[2]) / 100),
                heartbeatData[5], heartbeatData[6]
                )
            if(self.protocolVersion == 2):
                debugOutput += (" %02X%02X" % (heartbeatData[7], heartbeatData[8]))
            debugOutput += "  M"

            if(not fakeMaster):
                debugOutput += " %02X%02X" % (masterTWCID[0], masterTWCID[1])

            debugOutput += ": %02X %05.2f/%05.2fA %02X%02X" % \
                    (self.masterHeartbeatData[0],
                    (((self.masterHeartbeatData[3] << 8) + self.masterHeartbeatData[4]) / 100),
                    (((self.masterHeartbeatData[1] << 8) + self.masterHeartbeatData[2]) / 100),
                    self.masterHeartbeatData[5], self.masterHeartbeatData[6])
            if(self.protocolVersion == 2):
                debugOutput += (" %02X%02X" %
                    (self.masterHeartbeatData[7], self.masterHeartbeatData[8]))

            # Only output once-per-second heartbeat debug info when it's
            # different from the last output or if the only change has been amps
            # in use and it's only changed by 1.0 or less. Also output f it's
            # been 10 mins since the last output or if debugLevel is turned up
            # to 11.
            lastAmpsUsed = 0
            ampsUsed = 1
            debugOutputCompare = debugOutput
            m1 = re.search(r'SHB ....: .. (..\...)/', self.lastHeartbeatDebugOutput)
            if(m1):
                lastAmpsUsed = float(m1.group(1))
            m2 = re.search(r'SHB ....: .. (..\...)/', debugOutput)
            if(m2):
                ampsUsed = float(m2.group(1))
                if(m1):
                    debugOutputCompare = debugOutputCompare[0:m2.start(1)] + \
                        self.lastHeartbeatDebugOutput[m1.start(1):m1.end(1)] + \
                        debugOutputCompare[m2.end(1):]
            if(
                debugOutputCompare != self.lastHeartbeatDebugOutput
                or abs(ampsUsed - lastAmpsUsed) >= 1.0
                or time.time() - self.timeLastHeartbeatDebugOutput > 600
                or debugLevel >= 11
            ):
                print(time_now() + debugOutput)
                self.lastHeartbeatDebugOutput = debugOutput
                self.timeLastHeartbeatDebugOutput = time.time()
        except IndexError:
            # This happens if we try to access, say, heartbeatData[8] when
            # len(heartbeatData) < 9. This was happening due to a bug I fixed
            # but I may as well leave this here just in case.
            if(len(heartbeatData) != (7 if self.protocolVersion == 1 else 9)):
                print(time_now() + ': Error in print_status displaying heartbeatData',
                      heartbeatData, 'based on msg', hex_str(msg))
            if(len(self.masterHeartbeatData) != (7 if self.protocolVersion == 1 else 9)):
                print(time_now() + ': Error in print_status displaying masterHeartbeatData', self.masterHeartbeatData)

                
    def send_master_heartbeat(self):
        # Send our fake master's heartbeat to this TWCSlave.
        #
        # Heartbeat includes 7 bytes (Protocol 1) or 9 bytes (Protocol 2) of data
        # that we store in masterHeartbeatData.

        # Meaning of data:
        #
        # Byte 1 is a command:
        #   00 Make no changes
        #   02 Error
        #     Byte 2 appears to act as a bitmap where each set bit causes the
        #     slave TWC to enter a different error state. First 8 digits below
        #     show which bits are set and these values were tested on a Protocol
        #     2 TWC:
        #       0000 0001 = Middle LED blinks 3 times red, top LED solid green.
        #                   Manual says this code means 'Incorrect rotary switch
        #                   setting.'
        #       0000 0010 = Middle LED blinks 5 times red, top LED solid green.
        #                   Manual says this code means 'More than three Wall
        #                   Connectors are set to Slave.'
        #       0000 0100 = Middle LED blinks 6 times red, top LED solid green.
        #                   Manual says this code means 'The networked Wall
        #                   Connectors have different maximum current
        #                   capabilities.'
        #   	0000 1000 = No effect
        #   	0001 0000 = No effect
        #   	0010 0000 = No effect
        #   	0100 0000 = No effect
    	#       1000 0000 = No effect
        #     When two bits are set, the lowest bit (rightmost bit) seems to
        #     take precedence (ie 111 results in 3 blinks, 110 results in 5
        #     blinks).
        #
        #     If you send 02 to a slave TWC with an error code that triggers
        #     the middle LED to blink red, slave responds with 02 in its
        #     heartbeat, then stops sending heartbeat and refuses further
        #     communication. Slave's error state can be cleared by holding red
        #     reset button on its left side for about 4 seconds.
        #     If you send an error code with bitmap 11110xxx (where x is any bit),
        #     the error can not be cleared with a 4-second reset.  Instead, you
        #     must power cycle the TWC or 'reboot' reset which means holding
        #     reset for about 6 seconds till all the LEDs turn green.
        #   05 Tell slave charger to limit power to number of amps in bytes 2-3.
        #
        # Protocol 2 adds a few more command codes:
        #   06 Increase charge current by 2 amps.  Slave changes its heartbeat
        #      state to 06 in response. After 44 seconds, slave state changes to
        #      0A but amp value doesn't change.  This state seems to be used to
        #      safely creep up the amp value of a slave when the Master has extra
        #      power to distribute.  If a slave is attached to a car that doesn't
        #      want that many amps, Master will see the car isn't accepting the
        #      amps and stop offering more.  It's possible the 0A state change
        #      is not time based but rather indicates something like the car is
        #      now using as many amps as it's going to use.
        #   07 Lower charge current by 2 amps. Slave changes its heartbeat state
        #      to 07 in response. After 10 seconds, slave raises its amp setting
        #      back up by 2A and changes state to 0A.
        #      I could be wrong, but when a real car doesn't want the higher amp
        #      value, I think the TWC doesn't raise by 2A after 10 seconds. Real
        #      Master TWCs seem to send 07 state to all children periodically as
        #      if to check if they're willing to accept lower amp values. If
        #      they do, Master assigns those amps to a different slave using the
        #      06 state.
        #   08 Master acknowledges that slave stopped charging (I think), but
        #      the next two bytes contain an amp value the slave could be using.
        #   09 Tell slave charger to limit power to number of amps in bytes 2-3.
        #      This command replaces the 05 command in Protocol 1. However, 05
        #      continues to be used, but only to set an amp value to be used
        #      before a car starts charging. If 05 is sent after a car is
        #      already charging, it is ignored.
        #
        # Byte 2-3 is the max current a slave TWC can charge at in command codes
        # 05, 08, and 09. In command code 02, byte 2 is a bitmap. With other
        # command codes, bytes 2-3 are ignored.
        # If bytes 2-3 are an amp value of 0F A0, combine them as 0x0fa0 hex
        # which is 4000 in base 10. Move the decimal point two places left and
        # you get 40.00Amps max.
        #
        # Byte 4: 01 when a Master TWC is physically plugged in to a car.
        # Otherwise 00.
        #
        # Remaining bytes are always 00.
        #
        # Example 7-byte data that real masters have sent in Protocol 1:
        #   00 00 00 00 00 00 00  (Idle)
        #   02 04 00 00 00 00 00  (Error bitmap 04.  This happened when I
        #                         advertised a fake Master using an invalid max
        #                         amp value)
        #   05 0f a0 00 00 00 00  (Master telling slave to limit power to 0f a0
        #                         (40.00A))
        #   05 07 d0 01 00 00 00  (Master plugged in to a car and presumably
        #                          telling slaves to limit power to 07 d0
        #                          (20.00A). 01 byte indicates Master is plugged
        #                          in to a car.)
        global fakeTWCID, overrideMasterHeartbeatData, debugLevel, \
               timeLastTx

        if(len(overrideMasterHeartbeatData) >= 7):
            self.masterHeartbeatData = overrideMasterHeartbeatData

        newTWCID=hex_str(self.TWCID)
        
        if(newTWCID == '19 87'):
            if(stopCharge1987 == 0):
                send_msg(bytearray(b'\xFB\xE0') + fakeTWCID + bytearray(self.TWCID)
                         + bytearray(self.masterHeartbeatData))

                if(debugLevel >= 10):
                    print(time_now() + ': BUGFIX: -----inside heartbeet loop!!!--- ' + hex_str(self.TWCID))
                    print('stopcharge1987= '+ str(stopCharge1987))
                    
            else:
                self.reportedAmpsActual = 0
                self.reportedAmpsMax = 0
                print('BUGFIX: setting self.reportedAmpsActual to 0 for TWC '+ str(newTWCID))
                transmit_mqtt('TWC/ampsMax/' + hex_str(self.TWCID), self.reportedAmpsMax)
                transmit_mqtt('TWC/power/' + hex_str(self.TWCID), self.reportedAmpsActual)
                
        elif(newTWCID == '64 86'):
            if(stopCharge6486 == 0):
                send_msg(bytearray(b'\xFB\xE0') + fakeTWCID + bytearray(self.TWCID)
                         + bytearray(self.masterHeartbeatData))

                if(debugLevel >= 10):
                    print(time_now() + ': BUGFIX: -----inside heartbeet loop!!!--- ' + hex_str(self.TWCID))
                    print('stopcharge6486= '+ str(stopCharge6486))
            
            else:
                self.reportedAmpsActual = 0
                self.reportedAmpsMax = 0
                print('BUGFIX: setting self.reportedAmpsActual to 0 for TWC '+ str(newTWCID))
                transmit_mqtt('TWC/ampsMax/' + hex_str(self.TWCID), self.reportedAmpsMax)
                transmit_mqtt('TWC/power/' + hex_str(self.TWCID), self.reportedAmpsActual)


    def receive_slave_heartbeat(self, heartbeatData):
        # Handle heartbeat message received from real slave TWC.
        global debugLevel, nonScheduledAmpsMax, \
               maxAmpsToDivideAmongSlaves, wiringMaxAmpsAllTWCs, \
               timeLastGreenEnergyCheck, greenEnergyAmpsOffset, \
               slaveTWCRoundRobin, spikeAmpsToCancel6ALimit, \
               chargeNowAmps, chargeNowTimeEnd, minAmpsPerTWC

        now = time.time()
        self.timeLastRx = now
        
        self.reportedAmpsMax = ((heartbeatData[1] << 8) + heartbeatData[2]) / 100
        transmit_mqtt('TWC/ampsMax/' + hex_str(self.TWCID), self.reportedAmpsMax)
    
        self.reportedAmpsActual = ((heartbeatData[3] << 8) + heartbeatData[4]) / 100

        self.reportedState = heartbeatData[0]
        transmit_mqtt('TWC/state/' + hex_str(self.TWCID), self.reportedState)


        # self.lastAmpsOffered is initialized to -1.
        # If we find it at that value, set it to the current value reported by the
        # TWC.
        if(self.lastAmpsOffered < 0):
            self.lastAmpsOffered = self.reportedAmpsMax

        # Keep track of the amps the slave is actually using and the last time it
        # changed by more than 0.8A.
        # Also update self.reportedAmpsActualSignificantChangeMonitor if it's
        # still set to its initial value of -1.
        if(self.reportedAmpsActualSignificantChangeMonitor < 0
           or abs(self.reportedAmpsActual - self.reportedAmpsActualSignificantChangeMonitor) > 0.8
        ):
            self.timeReportedAmpsActualChangedSignificantly = now
            self.reportedAmpsActualSignificantChangeMonitor = self.reportedAmpsActual
            transmit_mqtt('TWC/power/' + hex_str(self.TWCID), self.reportedAmpsActual)

        ltNow = time.localtime()
        hourNow = ltNow.tm_hour + (ltNow.tm_min / 60)
        yesterday = ltNow.tm_wday - 1
        if(yesterday < 0):
            yesterday += 7

        # Check if it's time to resume tracking green energy.
        if(nonScheduledAmpsMax != -1 and hourResumeTrackGreenEnergy > -1
           and hourResumeTrackGreenEnergy == hourNow):
            nonScheduledAmpsMax = -1
            save_settings()

            if(debugLevel >= 10):
                print(time_now() + ': BUGFIX: nonScheduledAmpsMax = -1')

        # Check if we're within the hours we must use scheduledAmpsMax instead
        # of nonScheduledAmpsMax
        blnUseScheduledAmps = 0

        if(debugLevel >= 10):
            print(time_now() + ': BUGFIX: blnUseScheduledAmps = 0')

        if(scheduledAmpsMax > 0 and scheduledAmpsStartHour > -1
            and scheduledAmpsEndHour > -1 and scheduledAmpsDaysBitmap > 0):
		
            if(scheduledAmpsStartHour > scheduledAmpsEndHour):
                # We have a time like 8am to 7am which we must interpret as the
                # 23-hour period after 8am or before 7am. Since this case always
                # crosses midnight, we only ensure that scheduledAmpsDaysBitmap
                # is set for the day the period starts on. For example, if
                # scheduledAmpsDaysBitmap says only schedule on Monday, 8am to
                # 7am, we apply scheduledAmpsMax from Monday at 8am to Monday at
                # 11:59pm, and on Tuesday at 12am to Tuesday at 6:59am.
                if((hourNow >= scheduledAmpsStartHour 
		    and (scheduledAmpsDaysBitmap & (1 << ltNow.tm_wday)))
                    or (hourNow < scheduledAmpsEndHour and
                    (scheduledAmpsDaysBitmap & (1 << yesterday)))):
                        
                        blnUseScheduledAmps = 1

                        if(debugLevel >= 10):
                            print(time_now() + ': BUGFIX: blnUseScheduledAmps = 1 (23h period)')	
			
            else:
                # We have a time like 7am to 8am which we must interpret as the
                # 1-hour period between 7am and 8am.
                if(hourNow >= scheduledAmpsStartHour
                   and hourNow < scheduledAmpsEndHour
                   and (scheduledAmpsDaysBitmap & (1 << ltNow.tm_wday))):
                   
                    blnUseScheduledAmps = 1

                    if(debugLevel >= 10):
                        print(time_now() + ': BUGFIX: blnUseScheduledAmps = 1 (7 to 8)')

        if(chargeNowTimeEnd > 0 and chargeNowTimeEnd < now):
            # We're beyond the one-day period where we want to charge at
            # chargeNowAmps, so reset the chargeNow variables.
            chargeNowAmps = 0
            chargeNowTimeEnd = 0

        if(chargeNowTimeEnd > 0 and chargeNowAmps > 0):
            # We're still in the one-day period where we want to charge at
            # chargeNowAmps, ignoring all other charging criteria.
            maxAmpsToDivideAmongSlaves = chargeNowAmps
            if(debugLevel >= 10):
                print(time_now() + ': Charge at chargeNowAmps %.2f' % (chargeNowAmps))

        elif(blnUseScheduledAmps):
            # We're within the scheduled hours that we need to provide a set
            # number of amps.
            maxAmpsToDivideAmongSlaves = scheduledAmpsMax
        else:
            if(nonScheduledAmpsMax > -1):
                maxAmpsToDivideAmongSlaves = nonScheduledAmpsMax

                if(debugLevel >= 10):
                    print(time_now() + ': BUGFIX: maxAmpsToDivideAmongSlaves = nonScheduledAmpsMax')

            elif(now - timeLastGreenEnergyCheck > 30):
                timeLastGreenEnergyCheck = now

                # Don't bother to check solar generation before 6am or after
                # 8pm. Sunrise in most U.S. areas varies from a little before
                # 6am in Jun to almost 7:30am in Nov before the clocks get set
                # back an hour. Sunset can be ~4:30pm to just after 8pm.
                if(ltNow.tm_hour < 7 or ltNow.tm_hour >= 21):
                    maxAmpsToDivideAmongSlaves = 0

                else:
                    check_green_energy()


        # Use backgroundTasksLock to prevent the background thread from changing
        # the value of maxAmpsToDivideAmongSlaves after we've checked the value
        # is safe to use but before we've used it.


        if(maxAmpsToDivideAmongSlaves > wiringMaxAmpsAllTWCs):
            # Never tell the slaves to draw more amps than the physical charger
            # wiring can handle.
            if(debugLevel >= 1):
                print(time_now() +
                    " ERROR: maxAmpsToDivideAmongSlaves " + str(maxAmpsToDivideAmongSlaves) +
                    " > wiringMaxAmpsAllTWCs " + str(wiringMaxAmpsAllTWCs) +
                    ".\nSee notes above wiringMaxAmpsAllTWCs in the 'Configuration parameters' section.")
            maxAmpsToDivideAmongSlaves = wiringMaxAmpsAllTWCs

        # Determine how many cars are charging and how many amps they're using
        numCarsCharging = num_cars_charging_now()
        desiredAmpsOffered = maxAmpsToDivideAmongSlaves
        
        if (numCarsCharging > 0):
            for slaveTWC in slaveTWCRoundRobin:
                    if(slaveTWC.TWCID != self.TWCID):
                    # To avoid exceeding maxAmpsToDivideAmongSlaves, we must
                    # subtract the actual amps being used by this (other???) TWC from the amps
                    # we will offer.
                            desiredAmpsOffered -= slaveTWC.reportedAmpsActual

            # Allocate this slave a fraction of maxAmpsToDivideAmongSlaves divided
            # by the number of cars actually charging.
            fairShareAmps = (maxAmpsToDivideAmongSlaves / numCarsCharging)

            if(desiredAmpsOffered > fairShareAmps):
                    desiredAmpsOffered = fairShareAmps
                    if(debugLevel >= 10):
                        print("BUGFIX: desiredAmpsOffered = fairShareAmps: " +str(fairShareAmps) 
                              + "/// with maxAmpsToDivideAmongSlaves: " +str(maxAmpsToDivideAmongSlaves))

            if(debugLevel >= 10):
                print("desiredAmpsOffered TWC" + hex_str(self.TWCID) + " reduced from " + str(maxAmpsToDivideAmongSlaves)
                      + " to " + str(desiredAmpsOffered)
                      + " because of fairShareAmps " + str(fairShareAmps)
                      + " /// with " + str(numCarsCharging)
                      + " cars charging.")


        minAmpsToOffer = minAmpsPerTWC
        if(self.minAmpsTWCSupports > minAmpsToOffer):
            minAmpsToOffer = self.minAmpsTWCSupports
        
        
        if(desiredAmpsOffered < minAmpsToOffer):
            if(debugLevel >= 10):
                print("BUGFIX: desiredAmpsOffered (" + str(desiredAmpsOffered) + ") < minAmpsToOffer:" + str(minAmpsToOffer))
            if(numCarsCharging > 0):
                if(maxAmpsToDivideAmongSlaves / numCarsCharging > minAmpsToOffer):
                    # There is enough power available to give each car
                    # minAmpsToOffer, but currently-charging cars are leaving us
                    # less power than minAmpsToOffer to give this car.
                    #
                    # minAmpsToOffer is based on minAmpsPerTWC which is
                    # user-configurable, whereas self.minAmpsTWCSupports is based on
                    # the minimum amps TWC must be set to reliably start a car
                    # charging.
                    #
                    # Unfortunately, we can't tell if a car is plugged in or wanting
                    # to charge without offering it minAmpsTWCSupports. As the car
                    # gradually starts to charge, we will see it using power and
                    # tell other TWCs on the network to use less power. This could
                    # cause the sum of power used by all TWCs to exceed
                    # wiringMaxAmpsAllTWCs for a few seconds, but I don't think
                    # exceeding by up to minAmpsTWCSupports for such a short period
                    # of time will cause problems.
                    if(debugLevel >= 10):
                            print("desiredAmpsOffered TWC" + hex_str(self.TWCID) + " increased from " + str(desiredAmpsOffered)
                            + " to " + str(self.minAmpsTWCSupports)
                            + " (self.minAmpsTWCSupports)")
                    desiredAmpsOffered = self.minAmpsTWCSupports
                else:
                    if(debugLevel >= 10):
                        print("BUGFIX: maxAmpsToDivideAmongSlaves / numCarsCharging > minAmpsToOffer /// set desiredAmpsOffered to 0")
                    desiredAmpsOffered = 0
            else:
                if(debugLevel >= 10):
                    print("BUGFIX: numCarsCharging = 0 /// set desiredAmpsOffered to 0")
                desiredAmpsOffered = 0
                # There is not enough power available to give each car
                # minAmpsToOffer, so don't offer power to any cars. Alternately,
                # we could charge one car at a time and switch cars
                # periodically, but I'm not going to try to implement that.
                #
                # Note that 5A is the lowest value you can set using the Tesla car's
                # main screen, so lower values might have some adverse affect on the
                # car. I actually tried lower values when the sun was providing
                # under 5A of power and found the car would occasionally set itself
                # to state 03 and refuse to charge until you re-plugged the charger
                # cable. Clicking "Start charging" in the car's UI or in the phone
                # app would not start charging.
                #
                # A 5A charge only delivers ~3 miles of range to the car per hour,
                # but it forces the car to remain "on" at a level that it wastes
                # some power while it's charging. The lower the amps, the more power
                # is wasted. This is another reason not to go below 5A.
                #
                # So if there isn't at least 5A of power available, pass 0A as the
                # desired value. This tells the car to stop charging and it will
                # enter state 03 and go to sleep. You will hear the power relay in
                # the TWC turn off. When desiredAmpsOffered trends above 6A again,
                # it tells the car there's power.
                # If a car is set to energy saver mode in the car's UI, the car
                # seems to wake every 15 mins or so (unlocking or using phone app
                # also wakes it) and next time it wakes, it will see there's power
                # and start charging. Without energy saver mode, the car should
                # begin charging within about 10 seconds of changing this value.
                
            if(self.lastAmpsOffered > 0 
               and (now - self.timeLastAmpsOfferedChanged < 60
                    or now - self.timeReportedAmpsActualChangedSignificantly < 60
                    or self.reportedAmpsActual < 4.0)):
                    # We were previously telling the car to charge but now we want
                    # to tell it to stop. However, it's been less than a minute
                    # since we told it to charge or since the last significant
                    # change in the car's actual power draw or the car has not yet
                    # started to draw at least 5 amps (telling it 5A makes it
                    # actually draw around 4.18-4.27A so we check for
                    # self.reportedAmpsActual < 4.0).
                    #
                    # Once we tell the car to charge, we want to keep it going for
                    # at least a minute before turning it off again. concern is that
                    # yanking the power at just the wrong time during the
                    # start-charge negotiation could put the car into an error state
                    # where it won't charge again without being re-plugged. This
                    # concern is hypothetical and most likely could not happen to a
                    # real car, but I'd rather not take any chances with getting
                    # someone's car into a non-charging state so they're stranded
                    # when they need to get somewhere. Note that non-Tesla cars
                    # using third-party adapters to plug in are at a higher risk of
                    # encountering this sort of hypothetical problem.
                    #
                    # The other reason for this tactic is that in the minute we
                    # wait, desiredAmpsOffered might rise above 5A in which case we
                    # won't have to turn off the charger power at all. Avoiding too
                    # many on/off cycles preserves the life of the TWC's main power
                    # relay and may also prevent errors in the car that might be
                    # caused by turning its charging on and off too rapidly.
                    #
                    # Seeing self.reportedAmpsActual < 4.0 means the car hasn't
                    # ramped up to whatever level we told it to charge at last time.
                    # It may be asleep and take up to 15 minutes to wake up, see
                    # there's power, and start charging.
                    #
                    # Unfortunately, self.reportedAmpsActual < 4.0 can also mean the
                    # car is at its target charge level and may not accept power for
                    # days until the battery drops below a certain level. I can't
                    # think of a reliable way to detect this case. When the car
                    # stops itself from charging, we'll see self.reportedAmpsActual
                    # drop to near 0.0A and heartbeatData[0] becomes 03, but we can
                    # see the same 03 state when we tell the TWC to stop charging.
                    # We could record the time the car stopped taking power and
                    # assume it won't want more for some period of time, but we
                    # can't reliably detect if someone unplugged the car, drove it,
                    # and re-plugged it so it now needs power, or if someone plugged
                    # in a different car that needs power. Even if I see the car
                    # hasn't taken the power we've offered for the
                    # last hour, it's conceivable the car will reach a battery state
                    # where it decides it wants power the moment we decide it's safe
                    # to stop offering it. Thus, I think it's safest to always wait
                    # until the car has taken 5A for a minute before cutting power
                    # even if that means the car will charge for a minute when you
                    # first plug it in after a trip even at a time when no power
                    # should be available.
                    #
                    # One advantage of the above situation is that whenever you plug
                    # the car in, unless no power has been available since you
                    # unplugged, the charge port will turn green and start charging
                    # for a minute. This lets the owner quickly see that TWCManager
                    # is working properly each time they return home and plug in.
                if(debugLevel >= 10):
                        print("Don't stop charging TWC" + hex_str(self.TWCID) + " yet because: " +
                        'time - self.timeLastAmpsOfferedChanged ' +
                        str(int(now - self.timeLastAmpsOfferedChanged)) +
                        ' < 60 or time - self.timeReportedAmpsActualChangedSignificantly ' +
                        str(int(now - self.timeReportedAmpsActualChangedSignificantly)) +
                        ' < 60 or self.reportedAmpsActual ' + str(self.reportedAmpsActual) +
                        ' < 4')

                desiredAmpsOffered = minAmpsToOffer
                        
        else:
            if(debugLevel >= 10):
                print("BUGFIX: desiredAmpsOffered (" + str(desiredAmpsOffered) + ") > minAmpsToOffer:" + str(minAmpsToOffer) + "let's charge!!!")
            # We can tell the TWC how much power to use in 0.01A increments, but
            # the car will only alter its power in larger increments (somewhere
            # between 0.5 and 0.6A). The car seems to prefer being sent whole
            # amps and when asked to adjust between certain values like 12.6A
            # one second and 12.0A the next second, the car reduces its power
            # use to ~5.14-5.23A and refuses to go higher. So it seems best to
            # stick with whole amps.
            desiredAmpsOffered = int(desiredAmpsOffered)

            if(self.lastAmpsOffered == 0
               and now - self.timeLastAmpsOfferedChanged < 60
            ):
                # Keep charger off for at least 60 seconds before turning back
                # on. See reasoning above where I don't turn the charger off
                # till it's been on at least 60 seconds.
                if(debugLevel >= 10):
                    print("Don't start charging TWC" + hex_str(self.TWCID) + " yet because: " +
                          'self.lastAmpsOffered ' +
                          str(self.lastAmpsOffered) + " == 0 " +
                          'and time - self.timeLastAmpsOfferedChanged ' +
                          str(int(now - self.timeLastAmpsOfferedChanged)) +
                          " < 60")
                desiredAmpsOffered = self.lastAmpsOffered
            else:
                # Mid Oct 2017, Tesla pushed a firmware update to their cars
                # that seems to create the following bug:
                # If you raise desiredAmpsOffered AT ALL from the car's current
                # max amp limit, the car will drop its max amp limit to the 6A
                # setting (5.14-5.23A actual use as reported in
                # heartbeatData[2-3]). The odd fix to this problem is to tell
                # the car to raise to at least spikeAmpsToCancel6ALimit for 5 or
                # more seconds, then tell it to lower the limit to
                # desiredAmpsOffered. Even 0.01A less than
                # spikeAmpsToCancel6ALimit is not enough to cancel the 6A limit.
                #
                # I'm not sure how long we have to hold spikeAmpsToCancel6ALimit
                # but 3 seconds is definitely not enough but 5 seconds seems to
                # work. It doesn't seem to matter if the car actually hits
                # spikeAmpsToCancel6ALimit of power draw. In fact, the car is
                # slow enough to respond that even with 10s at 21A the most I've
                # seen it actually draw starting at 6A is 13A.
                if(debugLevel >= 10):
                    print('TWCID=' + hex_str(self.TWCID) + 
						  ' desiredAmpsOffered=' + str(desiredAmpsOffered) +
                          ' spikeAmpsToCancel6ALimit=' + str(spikeAmpsToCancel6ALimit) +
                          ' self.lastAmpsOffered=' + str(self.lastAmpsOffered) +
                          ' self.reportedAmpsActual=' + str(self.reportedAmpsActual) +
                          ' now - self.timeReportedAmpsActualChangedSignificantly=' +
                          str(int(now - self.timeReportedAmpsActualChangedSignificantly)))

                if(
                    # If we just moved from a lower amp limit to
                    # a higher one less than spikeAmpsToCancel6ALimit.
                   (
                     desiredAmpsOffered < spikeAmpsToCancel6ALimit
                       and
                     desiredAmpsOffered > self.lastAmpsOffered
                   )
                      or
                   (
                     # ...or if we've been offering the car more amps than it's
                     # been using for at least 10 seconds, then we'll change the
                     # amps we're offering it. For some reason, the change in
                     # amps offered will get the car to up its amp draw.
                     #
                     # First, check that the car is drawing enough amps to be
                     # charging...
                     self.reportedAmpsActual > 2.0
                       and
                     # ...and car is charging at under spikeAmpsToCancel6ALimit.
                     # I think I've seen cars get stuck between spikeAmpsToCancel6ALimit
                     # and lastAmpsOffered, but more often a car will be limited
                     # to under lastAmpsOffered by its UI setting or by the
                     # charger hardware it has on board, and we don't want to
                     # keep reducing it to spikeAmpsToCancel6ALimit.
                     # If cars really are getting stuck above
                     # spikeAmpsToCancel6ALimit, I may need to implement a
                     # counter that tries spikeAmpsToCancel6ALimit only a
                     # certain number of times per hour.
                     (self.reportedAmpsActual <= spikeAmpsToCancel6ALimit)
                       and
                     # ...and car is charging at over two amps under what we
                     # want it to charge at. I have to use 2 amps because when
                     # offered, say 40A, the car charges at ~38.76A actual.
                     # Using a percentage instead of 2.0A doesn't work because
                     # 38.58/40 = 95.4% but 5.14/6 = 85.6%
                     (self.lastAmpsOffered - self.reportedAmpsActual) > 2.0
                       and
                     # ...and car hasn't changed its amp draw significantly in
                     # over 10 seconds, meaning it's stuck at its current amp
                     # draw.
                     now - self.timeReportedAmpsActualChangedSignificantly > 10
                   )
                ):
                    # We must set desiredAmpsOffered to a value that gets
                    # reportedAmpsActual (amps the car is actually using) up to
                    # a value near lastAmpsOffered. At the end of all these
                    # checks, we'll set lastAmpsOffered = desiredAmpsOffered and
                    # timeLastAmpsOfferedChanged if the value of lastAmpsOffered was
                    # actually changed.
                    if(self.lastAmpsOffered == spikeAmpsToCancel6ALimit
                       and now - self.timeLastAmpsOfferedChanged > 10):
                        # We've been offering the car spikeAmpsToCancel6ALimit
                        # for over 10 seconds but it's still drawing at least
                        # 2A less than spikeAmpsToCancel6ALimit.  I saw this
                        # happen once when an error stopped the car from
                        # charging and when the error cleared, it was offered
                        # spikeAmpsToCancel6ALimit as the first value it saw.
                        # The car limited itself to 6A indefinitely. In this
                        # case, the fix is to offer it lower amps.
                        if(debugLevel >= 1):
                            print(time_now() + ': Car stuck when offered spikeAmpsToCancel6ALimit.  Offering 2 less.')
                        desiredAmpsOffered = spikeAmpsToCancel6ALimit - 2.0
                    elif(now - self.timeLastAmpsOfferedChanged > 5):
                        # self.lastAmpsOffered hasn't gotten the car to draw
                        # enough amps for over 5 seconds, so try
                        # spikeAmpsToCancel6ALimit
                        desiredAmpsOffered = spikeAmpsToCancel6ALimit
                    else:
                        # Otherwise, don't change the value of lastAmpsOffered.
                        desiredAmpsOffered = self.lastAmpsOffered

                    # Note that the car should have no problem increasing max
                    # amps to any whole value over spikeAmpsToCancel6ALimit as
                    # long as it's below any upper limit manually set in the
                    # car's UI. One time when I couldn't get TWC to push the car
                    # over 21A, I found the car's UI had set itself to 21A
                    # despite setting it to 40A the day before. I have been
                    # unable to reproduce whatever caused that problem.
                elif(desiredAmpsOffered < self.lastAmpsOffered):
                    # Tesla doesn't mind if we set a lower amp limit than the
                    # one we're currently using, but make sure we don't change
                    # limits more often than every 5 seconds. This has the side
                    # effect of holding spikeAmpsToCancel6ALimit set earlier for
                    # 5 seconds to make sure the car sees it.
                    if(debugLevel >= 10):
                        print('Reduce amps: time - self.timeLastAmpsOfferedChanged ' +
                            str(int(now - self.timeLastAmpsOfferedChanged)))
                    if(now - self.timeLastAmpsOfferedChanged < 5):
                        desiredAmpsOffered = self.lastAmpsOffered

        # set_last_amps_offered does some final checks to see if the new
        # desiredAmpsOffered is safe. It should be called after we've picked a
        # final value for desiredAmpsOffered.
        desiredAmpsOffered = self.set_last_amps_offered(desiredAmpsOffered)

        # See notes in send_slave_heartbeat() for details on how we transmit
        # desiredAmpsOffered and the meaning of the code in
        # self.masterHeartbeatData[0].
        #
        # Rather than only sending desiredAmpsOffered when slave is sending code
        # 04 or 08, it seems to work better to send desiredAmpsOffered whenever
        # it does not equal self.reportedAmpsMax reported by the slave TWC.
        # Doing it that way will get a slave charging again even when it's in
        # state 00 or 03 which it swings between after you set
        # desiredAmpsOffered = 0 to stop charging.
        #
        # I later found that a slave may end up swinging between state 01 and 03
        # when desiredAmpsOffered == 0:
        #   S 032e 0.25/0.00A: 01 0000 0019 0000  M: 00 0000 0000 0000
        #   S 032e 0.25/6.00A: 03 0258 0019 0000  M: 05 0000 0000 0000
        #   S 032e 0.25/0.00A: 01 0000 0019 0000  M: 00 0000 0000 0000
        #   S 032e 0.25/6.00A: 03 0258 0019 0000  M: 05 0000 0000 0000
        #
        # While it's doing this, it's continuously opening and closing the relay
        # on the TWC each second which makes an audible click and will wear out
        # the relay. To avoid that problem, always send code 05 when
        # desiredAmpsOffered == 0. In that case, slave's response should always
        # look like this:
        #   S 032e 0.25/0.00A: 03 0000 0019 0000 M: 05 0000 0000 0000
        if(self.reportedAmpsMax != desiredAmpsOffered
           or desiredAmpsOffered == 0
        ):
            desiredHundredthsOfAmps = int(desiredAmpsOffered * 100)
            self.masterHeartbeatData = bytearray([(0x09 if self.protocolVersion == 2 else 0x05),
              (desiredHundredthsOfAmps >> 8) & 0xFF,
              desiredHundredthsOfAmps & 0xFF,
              0x00,0x00,0x00,0x00,0x00,0x00])
        else:
            self.masterHeartbeatData = bytearray([0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00])

        if(len(overrideMasterHeartbeatData) >= 7):
            self.masterHeartbeatData = overrideMasterHeartbeatData

        if(debugLevel >= 1):
            self.print_status(heartbeatData)


    def set_last_amps_offered(self, desiredAmpsOffered):
        # self.lastAmpsOffered should only be changed using this sub.
        global debugLevel

        if(debugLevel >= 10):
            print("set_last_amps_offered (TWCID=" + hex_str(self.TWCID) +
                  ", desiredAmpsOffered=" + str(desiredAmpsOffered) + ")")

        if(desiredAmpsOffered != self.lastAmpsOffered):
            oldLastAmpsOffered = self.lastAmpsOffered
            self.lastAmpsOffered = desiredAmpsOffered

            # Set totalAmpsAllTWCs to the total amps all TWCs are actually using
            # minus amps this TWC is using, plus amps this TWC wants to use.
            totalAmpsAllTWCs = total_amps_actual_all_twcs() \
                  - self.reportedAmpsActual + self.lastAmpsOffered
            if(totalAmpsAllTWCs > wiringMaxAmpsAllTWCs):
                # totalAmpsAllTWCs would exceed wiringMaxAmpsAllTWCs if we
                # allowed this TWC to use desiredAmpsOffered.  Instead, try
                # offering as many amps as will increase total_amps_actual_all_twcs()
                # up to wiringMaxAmpsAllTWCs.
                self.lastAmpsOffered = int(wiringMaxAmpsAllTWCs -
                                          (total_amps_actual_all_twcs() - self.reportedAmpsActual))

                if(self.lastAmpsOffered < self.minAmpsTWCSupports):
                    # Always offer at least minAmpsTWCSupports amps.
                    # See notes in receive_slave_heartbeat() beneath
                    # 'if(maxAmpsToDivideAmongSlaves / numCarsCharging > minAmpsToOffer):'
                    self.lastAmpsOffered = self.minAmpsTWCSupports

                print("WARNING: Offering slave TWC %02X%02X %.1fA instead of " \
                    "%.1fA to avoid overloading wiring shared by all TWCs." % (
                    self.TWCID[0], self.TWCID[1], self.lastAmpsOffered, desiredAmpsOffered))

            if(self.lastAmpsOffered > self.wiringMaxAmps):
                # We reach this case frequently in some configurations, such as
                # when two 80A TWCs share a 125A line.  Therefore, don't print
                # an error.
                self.lastAmpsOffered = self.wiringMaxAmps
                if(debugLevel >= 10):
                    print("Offering slave TWC %02X%02X %.1fA instead of " \
                        "%.1fA to avoid overloading the TWC rated at %.1fA." % (
                        self.TWCID[0], self.TWCID[1], self.lastAmpsOffered,
                        desiredAmpsOffered, self.wiringMaxAmps))

            if(self.lastAmpsOffered != oldLastAmpsOffered):
                self.timeLastAmpsOfferedChanged = time.time()
        return self.lastAmpsOffered

#
# End TWCSlave class
#
##############################


##############################
#
# Begin global vars
#


data = ''
dataLen = 0
ignoredData = bytearray()
msg = bytearray()
msgLen = 0
lastTWCResponseMsg = None
overrideMasterHeartbeatData = b''

masterTWCID = ''
slaveHeartbeatData = bytearray([0x01,0x0F,0xA0,0x0F,0xA0,0x00,0x00,0x00,0x00])
numInitMsgsToSend = 10
msgRxCount = 0
timeLastTx = 0

slaveTWCs = {}
slaveTWCRoundRobin = []
idxSlaveToSendNextHeartbeat = 0

maxAmpsToDivideAmongSlaves = 0
scheduledAmpsMax = -1
scheduledAmpsStartHour = -1
scheduledAmpsEndHour = -1
scheduledAmpsDaysBitmap = 0x7F

chargeNowAmps = 0
chargeNowTimeEnd = 0

spikeAmpsToCancel6ALimit = 16
timeLastGreenEnergyCheck = 0
hourResumeTrackGreenEnergy = -1
kWhDelivered = 119
timeLastkWhDelivered = time.time()
timeLastkWhSaved = time.time()

# __FILE__ contains the path to the running script. Replace the script name with
# TWCManagerSettings.txt. This gives us a path that will always locate
# TWCManagerSettings.txt in the same directory as the script even when pwd does
# not match the script directory.
settingsFileName = re.sub(r'/[^/]+$', r'/TWCManagerSettings.txt', __file__)
nonScheduledAmpsMax = -1
timeLastHeartbeatDebugOutput = 0

stopCharge1987 = 0
stopCharge6486 = 0


webMsgPacked = ''
webMsgMaxSize = 300
webMsgResult = 0

timeTo0Aafter06 = 0
timeToRaise2A = 0


ser = None
ser = serial.Serial(rs485Adapter, baud, timeout=0)

#
# End global vars
#
##############################


##############################
#
# Begin main program
#

load_settings()


# Create an IPC (Interprocess Communication) message queue that we can
# periodically check to respond to queries from the TWCManager web interface.
#
# These messages will contain commands like "start charging at 10A" or may ask
# for information like "how many amps is the solar array putting out".
#
# The message queue is identified by a numeric key. This script and the web
# interface must both use the same key. The "ftok" function facilitates creating
# such a key based on a shared piece of information that is not likely to
# conflict with keys chosen by any other process in the system.
#
# ftok reads the inode number of the file or directory pointed to by its first
# parameter. This file or dir must already exist and the permissions on it don't
# seem to matter. The inode of a particular file or dir is fairly unique but
# doesn't change often so it makes a decent choice for a key.  We use the parent
# directory of the TWCManager script.
#
# The second parameter to ftok is a single byte that adds some additional
# uniqueness and lets you create multiple queues linked to the file or dir in
# the first param. We use 'T' for Tesla.
#
# If you can't get this to work, you can also set key = <some arbitrary number>
# and in the web interface, use the same arbitrary number. While that could
# conflict with another process, it's very unlikely to.
webIPCkey = sysv_ipc.ftok(re.sub('/[^/]+$', '/', __file__), ord('T'), True)

# Use the key to create a message queue with read/write access for all users.
webIPCqueue = sysv_ipc.MessageQueue(webIPCkey, sysv_ipc.IPC_CREAT, 0o666)
if(webIPCqueue == None):
    print("ERROR: Can't create Interprocess Communication message queue to communicate with web interface.")

# After the IPC message queue is created, if you type 'sudo ipcs -q' on the
# command like, you should see something like:
# ------ Message Queues --------
# key        msqid      owner      perms      used-bytes   messages
# 0x5402ed16 491520     pi         666        0            0
#
# Notice that we've created the only IPC message queue in the system. Apparently
# default software on the pi doesn't use IPC or if it does, it creates and
# deletes its message queues quickly.
#
# If you want to get rid of all queues because you created extras accidentally,
# reboot or type 'sudo ipcrm -a msg'.  Don't get rid of all queues if you see
# ones you didn't create or you may crash another process.
# Find more details in IPC here:
# http://www.onlamp.com/pub/a/php/2004/05/13/shared_memory.html


print("TWC Manager starting as fake %s with id %02X%02X and sign %02X" \
    % ( ("Master" if fakeMaster else "Slave"), \
    ord(fakeTWCID[0:1]), ord(fakeTWCID[1:2]), ord(slaveSign)))

while True:
    try:
        # In this area, we always send a linkready message when we first start.
        # Whenever there is no data available from other TWCs to respond to,
        # we'll loop back to this point to send another linkready or heartbeat
        # message. By only sending our periodic messages when no incoming
        # message data is available, we reduce the chance that we will start
        # transmitting a message in the middle of an incoming message, which
        # would corrupt both messages.

        # Add a 25ms sleep to prevent pegging pi's CPU at 100%. Lower CPU means
        # less power used and less waste heat.
        time.sleep(0.025)

        now = time.time()

        if(fakeMaster == 1):
            # A real master sends 5 copies of linkready1 and linkready2 whenever
            # it starts up, which we do here.
            # It doesn't seem to matter if we send these once per second or once
            # per 100ms so I do once per 100ms to get them over with.
            if(numInitMsgsToSend > 5):
                send_master_linkready1()
                time.sleep(0.1) # give slave time to respond
                numInitMsgsToSend -= 1
            elif(numInitMsgsToSend > 0):
                send_master_linkready2()
                time.sleep(0.1) # give slave time to respond
                numInitMsgsToSend = numInitMsgsToSend - 1
            else:
                # After finishing the 5 startup linkready1 and linkready2
                # messages, master will send a heartbeat message to every slave
                # it's received a linkready message from. Do that here.
                # A real master would keep sending linkready messages periodically
                # as long as no slave was connected, but since real slaves send
                # linkready once every 10 seconds till they're connected to a
                # master, we'll just wait for that.
                if(time.time() - timeLastTx >= 1.0):
                    # It's been about a second since our last heartbeat.
                    if(len(slaveTWCRoundRobin) > 0):
                        slaveTWC = slaveTWCRoundRobin[idxSlaveToSendNextHeartbeat]
                        if(time.time() - slaveTWC.timeLastRx > 26):
                            # A real master stops sending heartbeats to a slave
                            # that hasn't responded for ~26 seconds. It may
                            # still send the slave a heartbeat every once in
                            # awhile but we're just going to scratch the slave
                            # from our little black book and add them again if
                            # they ever send us a linkready.
                            print(time_now() + ": WARNING: We haven't heard from slave " \
                                "%02X%02X for over 26 seconds.  " \
                                "Stop sending them heartbeat messages." % \
                                (slaveTWC.TWCID[0], slaveTWC.TWCID[1]))
                            delete_slave(slaveTWC.TWCID)
                        else:
                            slaveTWC.send_master_heartbeat()

                        idxSlaveToSendNextHeartbeat = idxSlaveToSendNextHeartbeat + 1
                        if(idxSlaveToSendNextHeartbeat >= len(slaveTWCRoundRobin)):
                            idxSlaveToSendNextHeartbeat = 0
                        time.sleep(0.1) # give slave time to respond
        else:
            # As long as a slave is running, it sends link ready messages every
            # 10 seconds. They trigger any master on the network to handshake
            # with the slave and the master then sends a status update from the
            # slave every 1-3 seconds. Master's status updates trigger the slave
            # to send back its own status update.
            # As long as master has sent a status update within the last 10
            # seconds, slaves don't send link ready.
            # I've also verified that masters don't care if we stop sending link
            # ready as long as we send status updates in response to master's
            # status updates.
            if(fakeMaster != 2 and time.time() - timeLastTx >= 10.0):
                if(debugLevel >= 1):
                    print("Advertise fake slave %02X%02X with sign %02X is " \
                          "ready to link once per 10 seconds as long as master " \
                          "hasn't sent a heartbeat in the last 10 seconds." % \
                        (ord(fakeTWCID[0:1]), ord(fakeTWCID[1:2]), ord(slaveSign)))
                send_slave_linkready()


        ########################################################################
        # See if there's any message from the web interface.
        # If the message is longer than msgMaxSize, MSG_NOERROR tells it to
        # return what it can of the message and discard the rest.
        # When no message is available, IPC_NOWAIT tells msgrcv to return
        # msgResult = 0 and $! = 42 with description 'No message of desired
        # type'.
        # If there is an actual error, webMsgResult will be -1.
        # On success, webMsgResult is the length of webMsgPacked.
        try:
            webMsgRaw = webIPCqueue.receive(False, 2)
            if(len(webMsgRaw[0]) > 0):
                webMsgType = webMsgRaw[1]
                unpacked = struct.unpack('=LH', webMsgRaw[0][0:6])
                webMsgTime = unpacked[0]
                webMsgID = unpacked[1]
                webMsg = webMsgRaw[0][6:len(webMsgRaw[0])]

                if(debugLevel >= 1):
                    webMsgRedacted = webMsg

                    # Hide car password in web request to send password to Tesla
                    m = re.search(b'^(carApiEmailPassword=[^\n]+\n)', webMsg, re.MULTILINE)
                    if(m):
                        webMsgRedacted = m.group(1) + b'[HIDDEN]'
                    print(time_now() + ": Web query: '" + str(webMsgRedacted) + "', id " + str(webMsgID) +
                                       ", time " + str(webMsgTime) + ", type " + str(webMsgType))
                webResponseMsg = ''
                numPackets = 0
                if(webMsg == b'getStatus'):
                    
                    webResponseMsg = (
                        "%.2f" % (maxAmpsToDivideAmongSlaves) +
                        '`' + "%.2f" % (wiringMaxAmpsAllTWCs) +
                        '`' + "%.2f" % (minAmpsPerTWC) +
                        '`' + "%.2f" % (chargeNowAmps) +
                        '`' + str(nonScheduledAmpsMax) +
                        '`' + str(scheduledAmpsMax) +
                        '`' + "%02d:%02d" % (int(scheduledAmpsStartHour),
                                             int((scheduledAmpsStartHour % 1) * 60)) +
                        '`' + "%02d:%02d" % (int(scheduledAmpsEndHour),
                                             int((scheduledAmpsEndHour % 1) * 60)) +
                        '`' + str(scheduledAmpsDaysBitmap) +
                        '`' + "%02d:%02d" % (int(hourResumeTrackGreenEnergy),
                                             int((hourResumeTrackGreenEnergy % 1) * 60)) +
                        '`' + str(len(slaveTWCRoundRobin))
                        )

                    for i in range(0, len(slaveTWCRoundRobin)):
                        webResponseMsg += (
                            '`' + "%02X%02X" % (slaveTWCRoundRobin[i].TWCID[0],
                                                              slaveTWCRoundRobin[i].TWCID[1]) +
                            '~' + str(slaveTWCRoundRobin[i].maxAmps) +
                            '~' + "%.2f" % (slaveTWCRoundRobin[i].reportedAmpsActual) +
                            '~' + str(slaveTWCRoundRobin[i].lastAmpsOffered) +
                            '~' + str(slaveTWCRoundRobin[i].reportedState)
                            )

                elif(webMsg[0:20] == b'setNonScheduledAmps='):
                    m = re.search(b'([-0-9]+)', webMsg[19:len(webMsg)])
                    if(m):
                        nonScheduledAmpsMax = int(m.group(1))

                        # Save nonScheduledAmpsMax to SD card so the setting
                        # isn't lost on power failure or script restart.
                        save_settings()
                        
                elif(webMsg[0:15] == b'stopCharge1987='):
                    m = re.search(b'([-0-9]+)', webMsg[14:len(webMsg)])
                    if(m):
                        stopCharge1987 = int(m.group(1))

                        # Save stopCharge1987 to SD card so the setting
                        # isn't lost on power failure or script restart.
                        save_settings()
                        
                elif(webMsg[0:15] == b'stopCharge6486='):
                    m = re.search(b'([-0-9]+)', webMsg[14:len(webMsg)])
                    if(m):
                        stopCharge6486 = int(m.group(1))

                        # Save stopCharge1987 to SD card so the setting
                        # isn't lost on power failure or script restart.
                        save_settings()
                        
                elif(webMsg[0:17] == b'setScheduledAmps='):
                    m = re.search(b'([-0-9]+)\nstartTime=([-0-9]+):([0-9]+)\nendTime=([-0-9]+):([0-9]+)\ndays=([0-9]+)', \
                                  webMsg[17:len(webMsg)], re.MULTILINE)
                    if(m):
                        scheduledAmpsMax = int(m.group(1))
                        scheduledAmpsStartHour = int(m.group(2)) + (int(m.group(3)) / 60)
                        scheduledAmpsEndHour = int(m.group(4)) + (int(m.group(5)) / 60)
                        scheduledAmpsDaysBitmap = int(m.group(6))
                        save_settings()
                elif(webMsg[0:30] == b'setResumeTrackGreenEnergyTime='):
                    m = re.search(b'([-0-9]+):([0-9]+)', webMsg[30:len(webMsg)], re.MULTILINE)
                    if(m):
                        hourResumeTrackGreenEnergy = int(m.group(1)) + (int(m.group(2)) / 60)
                        save_settings()
                elif(webMsg[0:11] == b'sendTWCMsg='):
                    m = re.search(b'([0-9a-fA-F]+)', webMsg[11:len(webMsg)], re.MULTILINE)
                    if(m):
                        twcMsg = trim_pad(bytearray.fromhex(m.group(1).decode('ascii')),
                                          15 if len(slaveTWCRoundRobin) == 0 \
                                          or slaveTWCRoundRobin[0].protocolVersion == 2 else 13)
                        if((twcMsg[0:2] == b'\xFC\x19') or (twcMsg[0:2] == b'\xFC\x1A')):
                            print("\n*** ERROR: Web interface requested sending command:\n"
                                  + hex_str(twcMsg)
                                  + "\nwhich could permanently disable the TWC.  Aborting.\n")
                        elif((twcMsg[0:2] == b'\xFB\xE8')):
                            print("\n*** ERROR: Web interface requested sending command:\n"
                                  + hex_str(twcMsg)
                                  + "\nwhich could crash the TWC.  Aborting.\n")
                        else:
                            lastTWCResponseMsg = bytearray();
                            send_msg(twcMsg)
                elif(webMsg == b'getLastTWCMsgResponse'):
                    if(lastTWCResponseMsg != None and lastTWCResponseMsg != b''):
                        webResponseMsg = hex_str(lastTWCResponseMsg)
                    else:
                        webResponseMsg = 'None'

                elif(webMsg[0:23] == b'setMasterHeartbeatData='):
                    m = re.search(b'([0-9a-fA-F]*)', webMsg[23:len(webMsg)], re.MULTILINE)
                    if(m):
                        if(len(m.group(1)) > 0):
                            overrideMasterHeartbeatData = trim_pad(bytearray.fromhex(m.group(1).decode('ascii')),
                                                                   9 if slaveTWCRoundRobin[0].protocolVersion == 2 else 7)
                        else:
                            overrideMasterHeartbeatData = b''
                elif(webMsg == b'chargeNow'):
                    chargeNowAmps = wiringMaxAmpsAllTWCs
                    chargeNowTimeEnd = now + 60*60*24
                elif(webMsg == b'chargeNowCancel'):
                    chargeNowAmps = 0
                    chargeNowTimeEnd = 0
                elif(webMsg == b'dumpState'):
                    # dumpState commands are used for debugging. They are called
                    # using a web page:
                    # http://(Pi address)/index.php?submit=1&dumpState=1
                    webResponseMsg = ('time=' + str(now) + ', fakeMaster='
                        + str(fakeMaster) + ', rs485Adapter=' + rs485Adapter
                        + ', baud=' + str(baud)
                        + ', wiringMaxAmpsAllTWCs=' + str(wiringMaxAmpsAllTWCs)
                        + ', wiringMaxAmpsPerTWC=' + str(wiringMaxAmpsPerTWC)
                        + ', minAmpsPerTWC=' + str(minAmpsPerTWC)
                        + ', greenEnergyAmpsOffset=' + str(greenEnergyAmpsOffset)
                        + ', debugLevel=' + str(debugLevel)
                        + '\n')
                    

                    webResponseMsg += 'slaveTWCRoundRobin:\n'
                    for slaveTWC in slaveTWCRoundRobin:
                        webResponseMsg += str(slaveTWC.__dict__) + '\n'

                    numPackets = math.ceil(len(webResponseMsg) / 290)
                elif(webMsg[0:14] == b'setDebugLevel='):
                    m = re.search(b'([-0-9]+)', webMsg[14:len(webMsg)], re.MULTILINE)
                    if(m):
                        debugLevel = int(m.group(1))
                else:
                    print(time_now() + ": Unknown IPC request from web server: " + str(webMsg))

                if(len(webResponseMsg) > 0):
                    if(debugLevel >= 5):
                        print(time_now() + ": Web query response: '" + webResponseMsg + "'")

                    try:
                        if(numPackets == 0):
                            if(len(webResponseMsg) > 290):
                                webResponseMsg = webResponseMsg[0:290]

                            webIPCqueue.send(struct.pack('=LH' + str(len(webResponseMsg)) + 's', webMsgTime, webMsgID,
                                   webResponseMsg.encode('ascii')), block=False)
                        else:
                            # In this case, block=False prevents blocking if the message
                            # queue is too full for our message to fit. Instead, an
                            # error is returned.
                            msgTemp = struct.pack('=LH1s', webMsgTime, webMsgID, bytearray([numPackets]))
                            webIPCqueue.send(msgTemp, block=False)
                            for i in range(0, numPackets):
                                packet = webResponseMsg[i*290:i*290+290]
                                webIPCqueue.send(struct.pack('=LH' + str(len(packet)) + 's', webMsgTime, webMsgID,
                                   packet.encode('ascii')), block=False)

                    except sysv_ipc.BusyError:
                        print(time_now() + ": Error: IPC queue full when trying to send response to web interface.")

        except sysv_ipc.BusyError:
            # No web message is waiting.
            pass

        ########################################################################
        # See if there's an incoming message on the RS485 interface.

        timeMsgRxStart = time.time()
        while True:
            now = time.time()
            dataLen = ser.inWaiting()
            if(dataLen == 0):
                if(msgLen == 0):
                    # No message data waiting and we haven't received the
                    # start of a new message yet. Break out of inner while
                    # to continue at top of outer while loop where we may
                    # decide to send a periodic message.
                    break
                else:
                    # No message data waiting but we've received a partial
                    # message that we should wait to finish receiving.
                    if(now - timeMsgRxStart >= 2.0):
                        if(debugLevel >= 9):
                            print(time_now() + ": Msg timeout (" + hex_str(ignoredData) +
                                  ') ' + hex_str(msg[0:msgLen]))
                        msgLen = 0
                        ignoredData = bytearray()
                        break

                    time.sleep(0.025)
                    continue
            else:
                dataLen = 1
                data = ser.read(dataLen)

            if(dataLen != 1):
                # This should never happen
                print("WARNING: No data available.")
                break

            timeMsgRxStart = now
            timeLastRx = now
            if(msgLen == 0 and data[0] != 0xc0):
                # We expect to find these non-c0 bytes between messages, so
                # we don't print any warning at standard debug levels.
                if(debugLevel >= 11):
                    print("Ignoring byte %02X between messages." % (data[0]))
                ignoredData += data
                continue
            elif(msgLen > 0 and msgLen < 15 and data[0] == 0xc0):
                # If you see this when the program is first started, it
                # means we started listening in the middle of the TWC
                # sending a message so we didn't see the whole message and
                # must discard it. That's unavoidable.
                # If you see this any other time, it means there was some
                # corruption in what we received. It's normal for that to
                # happen every once in awhile but there may be a problem
                # such as incorrect termination or bias resistors on the
                # rs485 wiring if you see it frequently.
                if(debugLevel >= 10):
                    print("Found end of message before full-length message received.  " \
                          "Discard and wait for new message.")

                msg = data
                msgLen = 1
                continue

            if(msgLen == 0):
                msg = bytearray()
            msg += data
            msgLen += 1

            # Messages are usually 17 bytes or longer and end with \xc0\xfe.
            # However, when the network lacks termination and bias
            # resistors, the last byte (\xfe) may be corrupted or even
            # missing, and you may receive additional garbage bytes between
            # messages.
            #
            # TWCs seem to account for corruption at the end and between
            # messages by simply ignoring anything after the final \xc0 in a
            # message, so we use the same tactic. If c0 happens to be within
            # the corrupt noise between messages, we ignore it by starting a
            # new message whenever we see a c0 before 15 or more bytes are
            # received.
            #
            # Uncorrupted messages can be over 17 bytes long when special
            # values are "escaped" as two bytes. See notes in send_msg.
            #
            # To prevent most noise between messages, add a 120ohm
            # "termination" resistor in parallel to the D+ and D- lines.
            # Also add a 680ohm "bias" resistor between the D+ line and +5V
            # and a second 680ohm "bias" resistor between the D- line and
            # ground. See here for more information:
            #   https://www.ni.com/support/serial/resinfo.htm
            #   http://www.ti.com/lit/an/slyt514/slyt514.pdf
            # This explains what happens without "termination" resistors:
            #   https://e2e.ti.com/blogs_/b/analogwire/archive/2016/07/28/rs-485-basics-when-termination-is-necessary-and-how-to-do-it-properly
            if(msgLen >= 16 and data[0] == 0xc0):
                break

        if(msgLen >= 16):
            msg = unescape_msg(msg, msgLen)
            # Set msgLen = 0 at start so we don't have to do it on errors below.
            # len($msg) now contains the unescaped message length.
            msgLen = 0

            msgRxCount += 1

            # When the sendTWCMsg web command is used to send a message to the
            # TWC, it sets lastTWCResponseMsg = b''.  When we see that here,
            # set lastTWCResponseMsg to any unusual message received in response
            # to the sent message.  Never set lastTWCResponseMsg to a commonly
            # repeated message like master or slave linkready, heartbeat, or
            # voltage/kWh report.
            if(lastTWCResponseMsg == b''
               and msg[0:2] != b'\xFB\xE0' and msg[0:2] != b'\xFD\xE0'
               and msg[0:2] != b'\xFC\xE1' and msg[0:2] != b'\xFB\xE2'
               and msg[0:2] != b'\xFD\xE2' and msg[0:2] != b'\xFB\xEB'
               and msg[0:2] != b'\xFD\xEB' and msg[0:2] != b'\xFD\xE0'
            ):
                lastTWCResponseMsg = msg

            if(debugLevel >= 9):
                print("Rx@" + time_now() + ": (" + hex_str(ignoredData) + ') ' \
                      + hex_str(msg) + "")

            ignoredData = bytearray()

            # After unescaping special values and removing the leading and
            # trailing C0 bytes, the messages we know about are always 14 bytes
            # long in original TWCs, or 16 bytes in newer TWCs (protocolVersion
            # == 2).
            if(len(msg) != 14 and len(msg) != 16):
                print(time_now() + ": ERROR: Ignoring message of unexpected length %d: %s" % \
                       (len(msg), hex_str(msg)))
                continue

            checksumExpected = msg[len(msg) - 1]
            checksum = 0
            for i in range(1, len(msg) - 1):
                checksum += msg[i]

            if((checksum & 0xFF) != checksumExpected):
                print("ERROR: Checksum %X does not match %02X.  Ignoring message: %s" %
                    (checksum, checksumExpected, hex_str(msg)))
                continue

            if(fakeMaster == 1):
                ############################
                # Pretend to be a master TWC

                foundMsgMatch = False
                # We end each regex message search below with \Z instead of $
                # because $ will match a newline at the end of the string or the
                # end of the string (even without the re.MULTILINE option), and
                # sometimes our strings do end with a newline character that is
                # actually the CRC byte with a value of 0A or 0D.
                msgMatch = re.search(b'^\xfd\xe2(..)(.)(..)\x00\x00\x00\x00\x00\x00.+\Z', msg, re.DOTALL)
                if(msgMatch and foundMsgMatch == False):
                    # Handle linkready message from slave.
                    #
                    # We expect to see one of these before we start sending our
                    # own heartbeat message to slave.
                    # Once we start sending our heartbeat to slave once per
                    # second, it should no longer send these linkready messages.
                    # If slave doesn't hear master's heartbeat for around 10
                    # seconds, it sends linkready once per 10 seconds and starts
                    # flashing its red LED 4 times with the top green light on.
                    # Red LED stops flashing if we start sending heartbeat
                    # again.
                    foundMsgMatch = True
                    senderID = msgMatch.group(1)
                    sign = msgMatch.group(2)
                    maxAmps = ((msgMatch.group(3)[0] << 8) + msgMatch.group(3)[1]) / 100

                    if(debugLevel >= 1):
                        print(time_now() + ": %.2f amp slave TWC %02X%02X is ready to link.  Sign: %s" % \
                            (maxAmps, senderID[0], senderID[1],
                            hex_str(sign)))

                    if(maxAmps >= 80):
                        # U.S. chargers need a spike to 21A to cancel a 6A
                        # charging limit imposed in an Oct 2017 Tesla car
                        # firmware update. See notes where
                        # spikeAmpsToCancel6ALimit is used.
                        spikeAmpsToCancel6ALimit = 21
                    else:
                        # EU chargers need a spike to only 16A.  This value
                        # comes from a forum post and has not been directly
                        # tested.
                        spikeAmpsToCancel6ALimit = 16

                    if(senderID == fakeTWCID):
                        print(time_now + ": Slave TWC %02X%02X reports same TWCID as master.  " \
                              "Slave should resolve by changing its TWCID." % \
                              (senderID[0], senderID[1]))
                        # I tested sending a linkready to a real master with the
                        # same TWCID as master and instead of master sending back
                        # its heartbeat message, it sent 5 copies of its
                        # linkready1 and linkready2 messages. Those messages
                        # will prompt a real slave to pick a new random value
                        # for its TWCID.
                        #
                        # We mimic that behavior by setting numInitMsgsToSend =
                        # 10 to make the idle code at the top of the for()
                        # loop send 5 copies of linkready1 and linkready2.
                        numInitMsgsToSend = 10
                        continue

                    # We should always get this linkready message at least once
                    # and generally no more than once, so this is a good
                    # opportunity to add the slave to our known pool of slave
                    # devices.
                    slaveTWC = new_slave(senderID, maxAmps)

                    if(slaveTWC.protocolVersion == 1 and slaveTWC.minAmpsTWCSupports == 6):
                        if(len(msg) == 14):
                            slaveTWC.protocolVersion = 1
                            slaveTWC.minAmpsTWCSupports = 5
                        elif(len(msg) == 16):
                            slaveTWC.protocolVersion = 2
                            slaveTWC.minAmpsTWCSupports = 6

                        if(debugLevel >= 1):
                            print(time_now() + ": Set slave TWC %02X%02X protocolVersion to %d, minAmpsTWCSupports to %d." % \
                                 (senderID[0], senderID[1], slaveTWC.protocolVersion, slaveTWC.minAmpsTWCSupports))

                    # We expect maxAmps to be 80 on U.S. chargers and 32 on EU
                    # chargers. Either way, don't allow
                    # slaveTWC.wiringMaxAmps to be greater than maxAmps.
                    if(slaveTWC.wiringMaxAmps > maxAmps):
                        print("\n\n!!! DANGER DANGER !!!\nYou have set wiringMaxAmpsPerTWC to "
                              + str(wiringMaxAmpsPerTWC)
                              + " which is greater than the max "
                              + str(maxAmps) + " amps your charger says it can handle.  " \
                              "Please review instructions in the source code and consult an " \
                              "electrician if you don't know what to do.")
                        slaveTWC.wiringMaxAmps = maxAmps / 4

                    # Make sure we print one SHB message after a slave
                    # linkready message is received by clearing
                    # lastHeartbeatDebugOutput. This helps with debugging
                    # cases where I can't tell if we responded with a
                    # heartbeat or not.
                    slaveTWC.lastHeartbeatDebugOutput = ''

                    slaveTWC.timeLastRx = time.time()
                    slaveTWC.send_master_heartbeat()
                else:
                    msgMatch = re.search(b'\A\xfd\xe0(..)(..)(.......+?).\Z', msg, re.DOTALL)
                if(msgMatch and foundMsgMatch == False):
                    # Handle heartbeat message from slave.
                    #
                    # These messages come in as a direct response to each
                    # heartbeat message from master. Slave does not send its
                    # heartbeat until it gets one from master first.
                    # A real master sends heartbeat to a slave around once per
                    # second, so we do the same near the top of this for()
                    # loop. Thus, we should receive a heartbeat reply from the
                    # slave around once per second as well.
                    foundMsgMatch = True
                    senderID = msgMatch.group(1)
                    receiverID = msgMatch.group(2)
                    heartbeatData = msgMatch.group(3)

                    try:
                        slaveTWC = slaveTWCs[senderID]
                    except KeyError:
                        # Normally, a slave only sends us a heartbeat message if
                        # we send them ours first, so it's not expected we would
                        # hear heartbeat from a slave that's not in our list.
                        print(time_now() + ": ERROR: Received heartbeat message from " \
                                "slave %02X%02X that we've not met before." % \
                                (senderID[0], senderID[1]))
                        continue

                    if(fakeTWCID == receiverID):
                        slaveTWC.receive_slave_heartbeat(heartbeatData)
                    else:
                        # I've tried different fakeTWCID values to verify a
                        # slave will send our fakeTWCID back to us as
                        # receiverID. However, I once saw it send receiverID =
                        # 0000.
                        # I'm not sure why it sent 0000 and it only happened
                        # once so far, so it could have been corruption in the
                        # data or an unusual case.
                        if(debugLevel >= 1):
                            print(time_now() + ": WARNING: Slave TWC %02X%02X status data: " \
                                  "%s sent to unknown TWC %02X%02X." % \
                                (senderID[0], senderID[1],
                                hex_str(heartbeatData), receiverID[0], receiverID[1]))
                else:
                    msgMatch = re.search(b'\A\xfd\xeb(..)(..)(.+?).\Z', msg, re.DOTALL)
                if(msgMatch and foundMsgMatch == False):
                    # Handle kWh total and voltage message from slave.
                    #
                    # This message can only be generated by TWCs running newer
                    # firmware.  I believe it's only sent as a response to a
                    # message from Master in this format:
                    #   FB EB <Master TWCID> <Slave TWCID> 00 00 00 00 00 00 00 00 00
                    # Since we never send such a message, I don't expect a slave
                    # to ever send this message to us, but we handle it just in
                    # case.
                    # According to FuzzyLogic, this message has the following
                    # format on an EU (3-phase) TWC:
                    #   FD EB <Slave TWCID> 00000038 00E6 00F1 00E8 00
                    #   00000038 (56) is the total kWh delivered to cars
                    #     by this TWC since its construction.
                    #   00E6 (230) is voltage on phase A
                    #   00F1 (241) is voltage on phase B
                    #   00E8 (232) is voltage on phase C
                    #
                    # I'm guessing in world regions with two-phase power that
                    # this message would be four bytes shorter, but the pattern
                    # above will match a message of any length that starts with
                    # FD EB.
                    foundMsgMatch = True
                    senderID = msgMatch.group(1)
                    receiverID = msgMatch.group(2)
                    data = msgMatch.group(3)

                    if(debugLevel >= 1):
                        print(time_now() + ": Slave TWC %02X%02X unexpectedly reported kWh and voltage data: %s." % \
                            (senderID[0], senderID[1],
                            hex_str(data)))
                    
                else:
                    msgMatch = re.search(b'\A\xfc(\xe1|\xe2)(..)(.)\x00\x00\x00\x00\x00\x00\x00\x00.+\Z', msg, re.DOTALL)
                if(msgMatch and foundMsgMatch == False):
                    foundMsgMatch = True
                    print(time_now() + " ERROR: TWC is set to Master mode so it can't be controlled by TWCManager.  " \
                           "Search installation instruction PDF for 'rotary switch' and set " \
                           "switch so its arrow points to F on the dial.")
                if(foundMsgMatch == False):
                    print(time_now() + ": *** UNKNOWN MESSAGE FROM SLAVE:" + hex_str(msg)
                          + "\nPlease private message user CDragon at http://teslamotorsclub.com " \
                          "with a copy of this error.")
            
    except KeyboardInterrupt:
        print("Exiting after background tasks complete...")
        break

    except Exception as e:
        # Print info about unhandled exceptions, then continue.  Search for
        # 'Traceback' to find these in the log.
        traceback.print_exc()

        # Sleep 5 seconds so the user might see the error.
        time.sleep(5)


# Wait for background tasks thread to finish all tasks.
# Note that there is no such thing as backgroundTasksThread.stop(). Because we
# set the thread type to daemon, it will be automatically killed when we exit
# this program.

ser.close()

#
# End main program
#
##############################
