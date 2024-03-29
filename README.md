# linkmeddle
LINKed MEDia DL

linkmeddle.py had core code; most should work via that.  Some others require the other scripts.

## Dependencies
- See `requirements.txt`
- ffmpeg
- rabbitmq-server
- redis-server
- git (needed for development)
- python3-pip (needed for install)
- screen (recommended)

## Raspberry Pi hardware tips
* keep an eye on CPU temp with `$ vcgencmd measure_temp`; bad things seem to start happening around 65 degrees.
* Do use an SSD for initial download; lots of temp files etc
* Don't use some nonsense filesystem like NTFS; prefer native Linux.
* Do move swap to ssd instead of SD card - see `/etc/dphys-swapfile`; `CONF_SWAPFILE` and also set `CONF_SWAPSIZE` to 2048 (default is 100)
* Try not to kill multiple ffmpeg's at same time; cleanup is lots of CPU
* Try cutting dirty pages ratio in half - default is `vm.dirty_background_ratio = 10` and `vm.dirty_ratio = 20` - we set both to half by `/etc/sysctl.d/local.conf`
* see https://github.com/ctengel/objectindex README

## Uninstallation

Overwrite hard drive

```
sudo dd if=/dev/zero of=/mnt/abc/abc.dd bs=1048576 count=524288
sudo dd if=/dev/zero of=/dev/sda bs=1048576 count=524288 status=progress
sudo parted /dev/sda
sudo mkfs.ext4 /dev/sda1
```

Also hdparm can be investigated.
