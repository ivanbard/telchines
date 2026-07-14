# Medium RTL Build Integration

`filelists/soc.f` is a nested, ordered SoC build: it adds `include`, defines
`SIMULATION`, expands `filelists/core.f`, then compiles generated RTL and the
SoC wrapper. `core.f` places `common_pkg.sv` and `uart_pkg.sv` before modules
that import them.

`vendor/vendor_compile.f` is the vendor-style entrypoint. It adds the same
include directory, defines `VENDOR_PLL_MODEL=1`, invokes the nested SoC list,
and compiles the board wrapper. The generated `vendor_pll_wrapper.sv` includes
`vendor_build_defs.svh`, so missing include directories should be diagnosed
before a simulator is run.

The negative filelists intentionally cover two common failures:
`negative/missing_include.f` omits `+incdir+../include`, while
`negative/bad_order.f` imports `late_pkg` before compiling its package.
