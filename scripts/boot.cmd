echo "Running boot U-Boot script..."

# Load kernel and DTB from MMC
fatload mmc ${mmcdev}:${mmcpart} ${kernel_addr_r} uImage
fatload mmc ${mmcdev}:${mmcpart} ${fdt_addr_r} devicetree.dtb

# Override bootargs from DTB to ensure console on ttyPS0 (J2 header)
setenv bootargs 'console=ttyPS0,115200 root=/dev/mmcblk0p2 rootfstype=ext4 rootwait rw'

# Boot the kernel
bootm ${kernel_addr_r} - ${fdt_addr_r}
