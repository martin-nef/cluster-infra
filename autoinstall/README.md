# Autoinstall

## Building a bootable USB

1. Download [Ubuntu Server 24.04 LTS](https://ubuntu.com/download/server)
2. Flash the ISO to a USB drive (e.g. with `dd` or balenaEtcher)
3. Stamp the autoinstall for the new host
  - `./stamp.sh cloud-vm-1 ~/.ssh/cloud_vm_1_id_ed25519.pub`
5. Mount the USB and copy these files to the root of the USB:
  - `autoinstall.yaml`
6. Edit the `boot/grub/grub.cfg` file so it runs completely unattended

```diff
menuentry "Try or Install Ubuntu Server" {
	set gfxpayload=keep
-	linux	/casper/vmlinuz  ---
+	linux	/casper/vmlinuz autoinstall ---
	initrd	/casper/initrd
}
```

7. Boot the shitbox from the USB — the install runs unattended

## What the autoinstall does

- LVM storage layout using the full disk
- Creates a `deploy` user with SSH-only auth (password login disabled)
- Grants `deploy` passwordless sudo
- Installs OpenSSH server
