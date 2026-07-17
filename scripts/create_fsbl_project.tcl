source $env(XILINX_VITIS)/scripts/xsct/hsi/hsi.tcl

set hw_path [file normalize build/system_top.xsa]
hsi open_hw_design $hw_path
set cpu_name [lindex [hsi get_cells -filter {IP_TYPE==PROCESSOR}] 0]

hsi create_sw_design -name fsbl_design -proc $cpu_name -os standalone -app zynq_fsbl
hsi generate_app -dir [file normalize build/sdk/fsbl_app] -sw fsbl_design -compile

# Copy the generated elf to the expected location
file mkdir [file normalize build/sdk/fsbl]
file mkdir [file normalize build/sdk/fsbl/Release]
file copy -force [file normalize build/sdk/fsbl_app/executable.elf] [file normalize build/sdk/fsbl/Release/fsbl.elf]

hsi close_hw_design [hsi current_hw_design]
