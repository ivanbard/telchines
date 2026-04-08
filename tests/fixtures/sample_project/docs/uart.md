# UART Integration

`uart_rx.sv` should observe a start bit transition quickly after stimulus begins.
`uart_tx.sv` uses `tx_fifo_level` to determine when transmit data is available.
