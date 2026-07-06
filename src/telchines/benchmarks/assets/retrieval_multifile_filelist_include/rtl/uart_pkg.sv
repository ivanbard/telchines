package uart_pkg;
  parameter int UART_DATA_BITS = 8;
  parameter int UART_STOP_BITS = 1;

  typedef enum logic [1:0] {
    UART_IDLE,
    UART_START,
    UART_DATA,
    UART_STOP
  } uart_state_e;
endpackage
