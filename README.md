# linkmeddle
LINKed MEDia DL

## lm v3-4

### API/DB

`fastapi dev --port 29072`

### CLI

`python -m lmdb.cli --help`

### job runner all-in-one

v3 only

- Install deno
- setup venv?

```
pip install -U "yt-dlp[default]"
pip install sqlmodel  https://github.com/ctengel/yt-dlp-obj-idx/archive/master.zip https://github.com/ctengel/objectindex/archive/master.zip
OBJIDX_URL=http://127.0.0.1/ OBJIDX_AUTH=user python -m lmdb.run_bknd --oibucket bucket --no-playlist "https:/something.com/video"
```

### lmfe

`OBJIDX_URL= OBJIDX_AUTH= LINKMEDDLE_PLAPI= ~/venv/bin/fastapi dev lmfe/api.py fastapi --port 29062`

## Deno, etc

See the [yt-dlp EJS wiki](https://github.com/yt-dlp/yt-dlp/wiki/EJS)

### Installing

https://docs.deno.com/runtime/getting_started/installation/

From cargo on Fedora/RHEL (builds from source — takes a while):

```bash
dnf install cargo clang
cargo install deno --locked
ln -s ~/.cargo/bin/deno ~/.local/bin/deno   # or wherever is on your PATH
```

**Known issue** building Deno via cargo requires a recent `rustc`. If you see a compile error like:

```
error[E0658]: `let` expressions in this position are unstable
  --> .../v8-.../build.rs:1034:8
```

your rustc is too old to compile the `v8` crate that Deno depends on. Options:
- Install a newer rustc via [rustup](https://rust-lang.github.io/rustup/concepts/channels.html) (nightly or a recent stable)
- Use a prebuilt Deno binary from the official installer instead of cargo

### curl_cffi


```bash
pip install -U "yt-dlp[default,curl-cffi]"
```

See https://github.com/yt-dlp/yt-dlp#impersonation

## lm v1-2

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
