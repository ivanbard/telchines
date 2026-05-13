# Mixed Regression Context

The receive path can stall if the testbench never drives a start bit.
The transmit path requires `tx_fifo_level` to be declared and threaded through the DUT.
