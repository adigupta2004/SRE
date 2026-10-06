"""
Serial / Modbus RTU link setup. The only module that configures pyserial or
minimalmodbus; every device on the RS-485 bus is opened through here.
"""

import minimalmodbus

import config


def open_instrument(port=config.PORT_NAME, slave_address=config.SLAVE_ADDRESS):
    """
    Opens a Modbus RTU instrument with the shared serial settings.
    Raises (serial.SerialException etc.) if the port cannot be opened.
    """
    instrument = minimalmodbus.Instrument(port, slave_address)
    instrument.serial.baudrate = config.BAUDRATE
    instrument.serial.bytesize = config.BYTESIZE
    instrument.serial.parity = config.PARITY
    instrument.serial.stopbits = config.STOPBITS
    instrument.serial.timeout = config.TIMEOUT_S
    instrument.mode = minimalmodbus.MODE_RTU
    instrument.clear_buffers_before_each_transaction = True
    return instrument


def describe_link(port=config.PORT_NAME, slave_address=config.SLAVE_ADDRESS):
    """One-line summary of the link settings, for script banners."""
    return (
        f"Port: {port} | Slave: {slave_address} | Baud: {config.BAUDRATE} "
        f"| Format: {config.BYTESIZE}{config.PARITY}{config.STOPBITS} "
        f"| Timeout: {config.TIMEOUT_S:.1f} s"
    )
