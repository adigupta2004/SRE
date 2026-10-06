"""
Device code: everything that talks to or describes a piece of hardware.

    comms       serial / Modbus RTU link setup
    registers   Delta C2000 Modbus register map and command words
    c2000       C2000 driver: every read/write to the dyno VFD goes through here
    encoder     signed shaft RPM from the C2000's 32-bit encoder position
"""
