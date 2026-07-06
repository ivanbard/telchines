# UART Coverage Notes

The `start_bit_seen` functional bin is expected to hit whenever the UART RX
input observes a low start bit. A zero-hit bin should produce missing stimulus
guidance.
