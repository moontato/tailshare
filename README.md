# Tailshare

A terminal-based file-sharing utility for Tailscale networks. Simplifies file transfers between your Linux devices over Tailscale with an interactive TUI interface.

## Features

- **Auto-discovery**: Automatically discovers devices on your Tailscale network
- **TUI Interface**: Modern terminal UI with file browser, device list, and transfer queue
- **SFTP Transfers**: Secure file transfers using SSH/SFTP over Tailscale
- **Folder Support**: Recursively transfer folders with directory structure preserved
- **Progress Tracking**: Real-time transfer progress with speed and ETA
- **Multi-transfer**: Queue multiple transfers and let them run
- **No API Keys**: Uses `tailscale status` CLI command (no authentication required)

## Requirements

- Python 3.10 or higher
- Tailscale installed and running on source device
- SSH server running on target devices
- SSH key authentication configured (passwordless SSH)

## Installation

### Quick Install (Development)

```bash
# Clone the repository
git clone https://github.com/tailshare/tailshare.git
cd tailshare

# Install dependencies
pip install -r requirements.txt

# Run the application
python -m tailshare
```

### Install as Package

```bash
# Install in editable mode for development
pip install -e .

# Or install normally
pip install .

# Run the command
tailshare
```

### System Requirements

- Linux distribution (Ubuntu, Mint, Arch, etc.)
- Tailscale CLI installed (`tailscale` command in PATH)
- SSH client installed (`ssh` command in PATH)

## Usage

### Starting the Application

```bash
python -m tailshare
# or if installed as package:
tailshare
```

### TUI Controls

| Key | Action |
|-----|--------|
| `j/k` | Navigate file browser |
| `Enter` | Select file/enter directory |
| `r` | Refresh current view |
| `d` | Refresh device list |
| `s` | Send selected files |
| `c` | Clear completed transfers |
| `q` | Quit application |

### Workflow

1. **Discover Devices**: The app automatically discovers Tailscale devices on startup
2. **Select Target**: Click on a device in the device list (left panel)
3. **Browse Files**: Navigate to files/folders in the file browser (right panel)
4. **Set Remote Path**: Enter the destination path (defaults to `~` for home directory)
5. **Send**: Click "Send" to queue the transfer
6. **Monitor**: Watch transfer progress in the queue display

### Configuration

Create `~/.config/tailshare/config.yaml` to customize settings:

```yaml
ssh:
  key_paths:
    - ~/.ssh/id_ed25519  # Custom SSH key path (optional, uses defaults if empty)
  timeout: 30             # Connection timeout in seconds
  port: 22                # SSH port
ui:
  refresh_interval: 5     # Device list refresh interval in seconds
transfer:
  show_hidden_files: false  # Show hidden files in browser
```

### Logging

All transfer activities are logged to `~/.tailscale_share/log.txt`.

## SSH Setup

For passwordless transfers, set up SSH key authentication on target devices:

```bash
# On source device, generate SSH key if you don't have one
ssh-keygen -t ed25519 -C "tailshare"

# Copy public key to target device
ssh-copy-id user@target-device

# Test connection
ssh user@target-device
```

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
1. SSH server is running on target: `sudo systemctl status ssh`
2. SSH keys are set up for passwordless authentication
3. Target device is online in Tailscale

### No Devices Found

1. Verify Tailscale is running: `tailscale status`
2. Ensure other devices are online in your Tailscale network
3. Check firewall settings

## Testing

### Run Unit Tests

```bash
pytest tests/ -v
```

### Run with Coverage

```bash
pytest tests/ --cov=tailshare --cov-report=html
```

### Integration Testing

Integration tests require actual Tailscale devices. To manually test:

1. Set up two devices on the same Tailscale network
2. Configure SSH key authentication between them
3. Run tailshare on the source device
4. Transfer files and verify on target device

## Project Structure

```
tailshare/
├── __init__.py          # Package initialization
├── __main__.py          # Entry point
├── config.py            # Configuration and logging
├── devices.py           # Tailscale device discovery
├── transfer.py          # SFTP transfer logic
├── tui.py               # Textual TUI interface
├── pyproject.toml       # Package configuration
└── requirements.txt     # Dependencies

tests/
├── __init__.py
├── test_devices.py      # Device discovery tests
└── test_transfer.py     # Transfer logic tests
```

## Planned Features

These features are documented for future development but are **not implemented** in v1:

- **Resume Interrupted Transfers**: Checkpoint-based transfer resumption
- **End-to-End Encryption**: Additional encryption beyond Tailscale's encryption
- **Automatic Sync**: File/folder watching for automatic synchronization
- **Pull-based Transfers**: Target device can request files from source
- **Transfer Scheduling**: Queue transfers for specific times
- **Bandwidth Limiting**: Control transfer speed to avoid network saturation

## Security

- SSH key authentication only (no password storage)
- All file paths validated to prevent directory traversal
- Transfer activity logged for audit purposes
- Tailscale network encryption for all transfers

## License

MIT License - see LICENSE file for details.

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
