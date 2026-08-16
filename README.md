# Tailshare

A terminal-based file-sharing utility for Tailscale networks. Send and fetch files between your Linux devices over Tailscale with an interactive TUI.

![Tailshare screenshot](image.png)

## Features

- **Auto-discovery**: Automatically discovers devices on your Tailscale network (`tailscale status`, no API keys)
- **Bidirectional transfers**: Push files *to* a device (Send) or pull files *from* it (Fetch) via SFTP
- **TUI Interface**: Terminal UI with local and remote file browsers, device list, and per-tab transfer queues
- **Folder Support**: Recursively transfer folders with directory structure preserved
- **Progress Tracking**: Real-time transfer progress with speed and ETA
- **Multi-transfer**: Queue multiple transfers; tasks queued mid-run are picked up automatically
- **Per-tab status chips**: Each tab shows a live summary of its transfers (active / done / failed)

## Requirements

- Python 3.10 or higher
- Linux (Ubuntu, Mint, Arch, etc.)
- Tailscale installed and running on the device you run tailshare from
- SSH server running on target devices
- SSH authentication to target devices — either SSH keys (passwordless) or a password entered in the TUI

## Installation

```bash
# From the repository root
pip install .

# Run it
tailshare
# or
python -m tailshare
```

`requirements.txt` at the repository root lists the same dependencies plus the testing tools.

### CLI options

```bash
tailshare                        # normal start
tailshare --config PATH          # use a custom config file
tailshare --log-level DEBUG      # set log level (DEBUG/INFO/WARNING/ERROR)
tailshare --verbose              # DEBUG for tailshare and libraries
tailshare --version
```

## Usage

### Selecting a device

1. Start tailshare; the device list fills in from `tailscale status`.
2. Select a device in the left panel. The table uses a cell cursor: **click a row, then click it again (or press Enter)** to select it.
3. Optionally enter the remote **username** and **password** in the inputs below the device list. Leave both empty to use your local username and SSH key/agent authentication.
4. Press **Refresh** to rediscover devices and (re)connect the remote browser, or **Test** to check SSH connectivity to the selected device.

### Sending files (Send tab)

1. Navigate to the file or folder in the local file browser (`j`/`k`, `Enter`, `r` to refresh).
2. Enter the remote destination path (default `~` = the remote home directory). `~`-relative and relative paths are allowed; `..` traversal is rejected.
3. Click **Send** (or press `s`) to queue the transfer.

### Fetching files (Fetch tab)

1. Pick a device first — the remote file browser connects automatically and lists the remote home directory (click `..` to go up, `r` to refresh).
2. Select a file or directory in the remote browser.
3. Enter the local destination path (default `~`).
4. Click **Fetch** (or press `f`) to queue the download.

### TUI controls

| Key | Action |
|-----|--------|
| `j` / `k` | Navigate file browser |
| `Enter` | Select file / enter directory |
| `r` | Refresh current view |
| `R` | Refresh remote file browser (Fetch tab) |
| `d` | Refresh device list |
| `s` | Send selected files |
| `f` | Fetch selected files |
| `c` | Clear completed transfers |
| `q` | Quit application |

### Configuration

Create `~/.config/tailshare/config.yaml` to customize settings:

```yaml
ssh:
  key_paths:
    - ~/.ssh/id_ed25519   # Custom SSH key paths (optional; default keys if empty)
  user: null              # Remote SSH username (null = local username)
  timeout: 30             # Connection timeout in seconds
  port: 22                # SSH port
```

Values are deep-merged over the defaults, so a partial file is fine.

### Logging

All activity is logged to `~/.tailscale_share/log.txt` (INFO by default).

## SSH Setup

Passwordless key authentication is the smoothest option on target devices:

```bash
# On the device you run tailshare from
ssh-keygen -t ed25519 -C "tailshare"
ssh-copy-id user@target-device
ssh user@target-device   # verify
```

If you can't set up keys, just enter the target's username and password in the TUI — the password is held in memory for the duration of the session, never written to disk.

## Troubleshooting

### Tailscale Not Running

```
Error: Tailscale is not running. Run: sudo tailscale up
```

Start Tailscale:

```bash
sudo tailscale up
```

### SSH Connection Failed

Ensure:
1. SSH server is running on the target: `sudo systemctl status ssh`
2. The username/password (or SSH keys) are correct
3. The target device is online in Tailscale

### No Devices Found

1. Verify Tailscale is running: `tailscale status`
2. Ensure other devices are online in your Tailscale network
3. Check firewall settings

## Testing

The test suite is hermetic (no Tailscale or SSH servers required):

```bash
pytest tests/ -v
pytest tests/ --cov=tailshare --cov-report=html
```

For a real end-to-end check, set up two devices on the same tailnet, run tailshare on one, and send a file in both directions.

## Project Structure

```
.
├── pyproject.toml           # Package configuration
├── requirements.txt         # Dependencies (runtime + testing)
├── LICENSE                  # MIT License
├── image.png                # Screenshot
├── tailshare/
│   ├── __init__.py          # Package initialization
│   ├── __main__.py          # CLI entry point
│   ├── config.py            # Configuration, logging, path validation
│   ├── devices.py           # Tailscale device discovery
│   ├── transfer.py          # SFTP transfers and queue management
│   └── tui.py               # Textual TUI
└── tests/
    ├── test_config.py       # Configuration and logging tests
    ├── test_devices.py      # Device discovery tests
    ├── test_main.py         # CLI entry point tests
    ├── test_sftp_client.py  # SFTP client + queue manager (fake SFTP)
    ├── test_tui_connect.py  # TUI device selection and connect lifecycle
    ├── test_tui_smoke.py    # TUI smoke tests
    ├── test_transfer.py     # Progress/task/manager tests
    └── test_transfer_validation.py  # Path validation across directions
```

## Planned Features

Documented for future development, **not implemented**:

- **Resume Interrupted Transfers**: Checkpoint-based transfer resumption
- **End-to-End Encryption**: Additional encryption beyond Tailscale's encryption
- **Automatic Sync**: File/folder watching for automatic synchronization
- **Transfer Scheduling**: Queue transfers for specific times
- **Bandwidth Limiting**: Control transfer speed to avoid network saturation

## Security

- **Authentication**: SSH key or password. Passwords are held in memory only — they are never written to disk, logs, or object reprs.
- **Path validation**: All local and remote paths are validated before use; directory-traversal sequences (`..`) are rejected on the raw input before any expansion or normalization, in both transfer directions.
- **Host key verification**: `paramiko.AutoAddPolicy()` is used, so remote host keys are **not** verified. This is an accepted trade-off on a WireGuard-encrypted tailnet where all traffic is already authenticated and encrypted end-to-end; if you need host key pinning, extend `SFTPClient.connect()`.
- **Auditing**: Transfer activity is logged to `~/.tailscale_share/log.txt`.

## License

MIT License — see the [LICENSE](LICENSE) file.

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Write tests for new functionality
4. Submit a pull request

## Acknowledgments

- [Textual](https://github.com/Textualize/textual) - TUI framework
- [Paramiko](https://www.paramiko.org/) - SSH/SFTP library
- [Tailscale](https://tailscale.com/) - Network infrastructure
