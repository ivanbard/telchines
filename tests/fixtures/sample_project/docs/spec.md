# Broken Counter

The counter clears to zero when `rst_n` is deasserted and increments on each clock edge after reset is released.

# UART Notes

The UART receiver waits for a valid start bit before sampling incoming data.
The UART transmitter derives its ready behavior from the FIFO fill level.
