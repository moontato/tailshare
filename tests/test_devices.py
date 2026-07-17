"""Tests for device discovery and parsing."""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock

from tailshare.devices import (
    Device,
    DeviceDiscovery,
    TailscaleNotRunningError,
    DeviceDiscoveryError,
)


class TestDevice:
    """Tests for the Device dataclass."""
    
    def test_device_creation(self) -> None:
        """Test creating a device with all fields."""
        device = Device(
            name="test-pc",
            hostname="test-pc.local",
            ip="100.64.0.1",
            online=True,
            last_seen="2024-01-01T00:00:00Z",
            machine_id="machine123",
        )
        
        assert device.name == "test-pc"
        assert device.hostname == "test-pc.local"
        assert device.ip == "100.64.0.1"
        assert device.online is True
        assert device.last_seen == "2024-01-01T00:00:00Z"
        assert device.machine_id == "machine123"
    
    def test_device_str_online(self) -> None:
        """Test string representation of online device."""
        device = Device(
            name="test-pc",
            hostname="test-pc.local",
            ip="100.64.0.1",
            online=True,
            last_seen=None,
            machine_id="",
        )
        
        assert "online" in str(device)
        assert "100.64.0.1" in str(device)
    
    def test_device_str_offline(self) -> None:
        """Test string representation of offline device."""
        device = Device(
            name="test-pc",
            hostname="test-pc.local",
            ip="100.64.0.1",
            online=False,
            last_seen=None,
            machine_id="",
        )
        
        assert "offline" in str(device)
    
    def test_device_to_dict(self) -> None:
        """Test converting device to dictionary."""
        device = Device(
            name="test-pc",
            hostname="test-pc.local",
            ip="100.64.0.1",
            online=True,
            last_seen="2024-01-01T00:00:00Z",
            machine_id="machine123",
        )
        
        device_dict = device.to_dict()
        
        assert device_dict["name"] == "test-pc"
        assert device_dict["ip"] == "100.64.0.1"
        assert device_dict["online"] is True
    
    def test_device_from_dict(self) -> None:
        """Test creating device from dictionary."""
        device_dict = {
            "name": "test-pc",
            "hostname": "test-pc.local",
            "ip": "100.64.0.1",
            "online": True,
            "last_seen": "2024-01-01T00:00:00Z",
            "machine_id": "machine123",
        }
        
        device = Device.from_dict(device_dict)
        
        assert device.name == "test-pc"
        assert device.ip == "100.64.0.1"
        assert device.online is True
    
    def test_device_from_dict_defaults(self) -> None:
        """Test creating device from incomplete dictionary."""
        device_dict = {}
        
        device = Device.from_dict(device_dict)
        
        assert device.name == "unknown"
        assert device.ip == "0.0.0.0"
        assert device.online is False


class TestDeviceDiscovery:
    """Tests for device discovery parsing."""
    
    @pytest.fixture
    def discovery(self) -> DeviceDiscovery:
        """Create a DeviceDiscovery instance."""
        return DeviceDiscovery()
    
    def test_parse_status_with_peers(self, discovery: DeviceDiscovery) -> None:
        """Test parsing status with multiple peers."""
        status_data = {
            "Self": {
                "Name": "source-machine",
                "PrimaryIPs": ["100.64.0.10"],
            },
            "Peers": [
                {
                    "Name": "dest-pc.tailnet.ts.net",
                    "HostName": "dest-pc",
                    "PrimaryIPs": ["100.64.0.20"],
                    "Online": True,
                    "LastSeen": "2024-01-01T00:00:00Z",
                    "ID": "machine456",
                },
                {
                    "Name": "server.tailnet.ts.net",
                    "HostName": "server",
                    "PrimaryIPs": ["100.64.0.30"],
                    "Online": False,
                    "LastSeen": "2024-01-02T00:00:00Z",
                    "ID": "machine789",
                },
            ],
        }
        
        devices = discovery._parse_status(status_data)
        
        assert len(devices) == 2
        
        # Check first device
        assert devices[0].name == "dest-pc"
        assert devices[0].hostname == "dest-pc"
        assert devices[0].ip == "100.64.0.20"
        assert devices[0].online is True
        
        # Check second device
        assert devices[1].name == "server"
        assert devices[1].ip == "100.64.0.30"
        assert devices[1].online is False
    
    def test_parse_status_excludes_self(self, discovery: DeviceDiscovery) -> None:
        """Test that own device is excluded from peer list."""
        status_data = {
            "Self": {
                "Name": "source-machine",
                "PrimaryIPs": ["100.64.0.10"],
            },
            "Peers": [
                {
                    "Name": "source-machine.tailnet.ts.net",
                    "HostName": "source-machine",
                    "PrimaryIPs": ["100.64.0.10"],
                    "Online": True,
                    "ID": "self-machine",
                },
                {
                    "Name": "other-pc.tailnet.ts.net",
                    "HostName": "other-pc",
                    "PrimaryIPs": ["100.64.0.20"],
                    "Online": True,
                    "ID": "other-machine",
                },
            ],
        }
        
        devices = discovery._parse_status(status_data)
        
        # Should only have one device (not self)
        assert len(devices) == 1
        assert devices[0].name == "other-pc"
    
    def test_parse_status_empty_peers(self, discovery: DeviceDiscovery) -> None:
        """Test parsing status with no peers."""
        status_data = {
            "Self": {
                "Name": "source-machine",
                "PrimaryIPs": ["100.64.0.10"],
            },
            "Peers": [],
        }
        
        devices = discovery._parse_status(status_data)
        
        assert len(devices) == 0
    
    def test_parse_status_no_primary_ips(self, discovery: DeviceDiscovery) -> None:
        """Test that peers without IPs are skipped."""
        status_data = {
            "Self": {
                "Name": "source-machine",
                "PrimaryIPs": ["100.64.0.10"],
            },
            "Peers": [
                {
                    "Name": "incomplete.tailnet.ts.net",
                    "HostName": "incomplete",
                    "PrimaryIPs": [],
                    "Online": True,
                    "ID": "incomplete",
                },
            ],
        }
        
        devices = discovery._parse_status(status_data)
        
        assert len(devices) == 0
    
    def test_get_online_devices(self, discovery: DeviceDiscovery) -> None:
        """Test filtering online devices."""
        discovery._devices = [
            Device(
                name="online-pc",
                hostname="online-pc",
                ip="100.64.0.1",
                online=True,
                last_seen=None,
                machine_id="",
            ),
            Device(
                name="offline-pc",
                hostname="offline-pc",
                ip="100.64.0.2",
                online=False,
                last_seen=None,
                machine_id="",
            ),
        ]
        
        online = discovery.get_online_devices()
        
        assert len(online) == 1
        assert online[0].name == "online-pc"
    
    def test_get_device_by_ip(self, discovery: DeviceDiscovery) -> None:
        """Test finding device by IP."""
        discovery._devices = [
            Device(
                name="test-pc",
                hostname="test-pc",
                ip="100.64.0.1",
                online=True,
                last_seen=None,
                machine_id="",
            ),
        ]
        
        device = discovery.get_device_by_ip("100.64.0.1")
        
        assert device is not None
        assert device.name == "test-pc"
    
    def test_get_device_by_ip_not_found(self, discovery: DeviceDiscovery) -> None:
        """Test finding non-existent device by IP."""
        discovery._devices = []
        
        device = discovery.get_device_by_ip("100.64.0.99")
        
        assert device is None
    
    def test_get_device_by_name(self, discovery: DeviceDiscovery) -> None:
        """Test finding device by name."""
        discovery._devices = [
            Device(
                name="test-pc",
                hostname="test-pc",
                ip="100.64.0.1",
                online=True,
                last_seen=None,
                machine_id="",
            ),
        ]
        
        device = discovery.get_device_by_name("test-pc")
        
        assert device is not None
        assert device.ip == "100.64.0.1"
    
    def test_get_device_by_name_case_insensitive(self, discovery: DeviceDiscovery) -> None:
        """Test that name search is case-insensitive."""
        discovery._devices = [
            Device(
                name="Test-PC",
                hostname="Test-PC",
                ip="100.64.0.1",
                online=True,
                last_seen=None,
                machine_id="",
            ),
        ]
        
        device = discovery.get_device_by_name("test-pc")
        
        assert device is not None


class TestDeviceDiscoveryIntegration:
    """Integration tests for device discovery (mocked)."""
    
    def test_discover_success(self) -> None:
        """Test successful device discovery."""
        mock_output = json.dumps({
            "Self": {
                "Name": "source-machine",
                "PrimaryIPs": ["100.64.0.10"],
            },
            "Peers": [
                {
                    "Name": "dest-pc.tailnet.ts.net",
                    "HostName": "dest-pc",
                    "PrimaryIPs": ["100.64.0.20"],
                    "Online": True,
                    "ID": "machine123",
                },
            ],
        })
        
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=mock_output,
                stderr="",
            )
            
            discovery = DeviceDiscovery()
            devices = discovery.discover()
            
            assert len(devices) == 1
            assert devices[0].name == "dest-pc"
    
    def test_discover_tailscale_not_running(self) -> None:
        """Test error when tailscale is not running."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="",
                stderr="tailscale not running",
            )
            
            discovery = DeviceDiscovery()
            
            with pytest.raises(TailscaleNotRunningError):
                discovery.discover()
    
    def test_discover_command_not_found(self) -> None:
        """Test error when tailscale command not found."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("tailscale")
            
            discovery = DeviceDiscovery()
            
            with pytest.raises(TailscaleNotRunningError):
                discovery.discover()
    
    def test_discover_invalid_json(self) -> None:
        """Test error when output is invalid JSON."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="invalid json",
                stderr="",
            )
            
            discovery = DeviceDiscovery()
            
            with pytest.raises(DeviceDiscoveryError):
                discovery.discover()
