"""Tailscale device discovery and management.

This module handles:
- Parsing `tailscale status --json` output
- Device information extraction
- Device list management
- Tailscale status checking
"""

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass
class Device:
    """Represents a Tailscale device.
    
    Attributes:
        name: Device name (e.g., "work-pc")
        hostname: Device hostname
        ip: Primary Tailscale IP address
        online: Whether device is currently online
        last_seen: Last seen timestamp string
        machine_id: Unique machine identifier
    """
    
    name: str
    hostname: str
    ip: str
    online: bool
    last_seen: str | None
    machine_id: str
    
    def __str__(self) -> str:
        """Return user-friendly string representation."""
        status = "online" if self.online else "offline"
        return f"{self.name} ({self.ip}) [{status}]"
    
    def to_dict(self) -> dict[str, Any]:
        """Convert device to dictionary.
        
        Returns:
            Dictionary representation of device
        """
        return {
            "name": self.name,
            "hostname": self.hostname,
            "ip": self.ip,
            "online": self.online,
            "last_seen": self.last_seen,
            "machine_id": self.machine_id,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Device":
        """Create device from dictionary.
        
        Args:
            data: Dictionary with device information
            
        Returns:
            Device instance
        """
        return cls(
            name=data.get("name", "unknown"),
            hostname=data.get("hostname", "unknown"),
            ip=data.get("ip", "0.0.0.0"),
            online=data.get("online", False),
            last_seen=data.get("last_seen"),
            machine_id=data.get("machine_id", ""),
        )


class DeviceDiscoveryError(Exception):
    """Exception raised when device discovery fails."""
    pass


class TailscaleNotRunningError(DeviceDiscoveryError):
    """Exception raised when Tailscale is not running."""
    pass


class DeviceDiscovery:
    """Discovers and manages Tailscale devices.
    
    Uses `tailscale status --json` to discover devices on the
    Tailscale network. No API authentication required.
    
    Attributes:
        devices: List of discovered devices
        self_ip: This device's Tailscale IP
    """
    
    def __init__(self) -> None:
        """Initialize device discovery."""
        self._devices: list[Device] = []
        self._self_ip: str = ""
        self._logger = logging.getLogger(__name__)
    
    def discover(self) -> list[Device]:
        """Discover devices on the Tailscale network.
        
        Runs `tailscale status --json` and parses the output.
        
        Returns:
            List of discovered devices
            
        Raises:
            TailscaleNotRunningError: If tailscale command fails
            DeviceDiscoveryError: If output parsing fails
        """
        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            raise TailscaleNotRunningError(
                "Tailscale command not found. Please install Tailscale."
            )
        except subprocess.TimeoutExpired:
            raise DeviceDiscoveryError("Timeout while discovering devices")
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            if "not running" in error_msg.lower() or "not connected" in error_msg.lower():
                raise TailscaleNotRunningError(
                    "Tailscale is not running. Run: sudo tailscale up"
                )
            raise DeviceDiscoveryError(
                f"Failed to discover devices: {error_msg}"
            )
        
        try:
            status_data = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise DeviceDiscoveryError(f"Failed to parse tailscale status: {e}")
        
        return self._parse_status(status_data)
    
    def _parse_status(self, status_data: dict[str, Any]) -> list[Device]:
        """Parse tailscale status JSON output.
        
        Args:
            status_data: JSON data from tailscale status
            
        Returns:
            List of Device instances
        """
        devices: list[Device] = []
        
        # Extract this device's IP
        self_data = status_data.get("Self") or status_data.get("self")
        if self_data:
            self_ips = (
                self_data.get("TailscaleIPs") or 
                self_data.get("primary_ips") or 
                self_data.get("PrimaryIPs") or 
                self_data.get("ips") or 
                self_data.get("IP") or 
                self_data.get("ip")
            )
            if self_ips:
                if isinstance(self_ips, list):
                    self._self_ip = self_ips[0] if self_ips else ""
                else:
                    self._self_ip = self_ips

        
        # Parse peer devices
        peers_data = status_data.get("Peer") or status_data.get("Peers") or status_data.get("peers", [])
        
        if isinstance(peers_data, dict):
            peers = peers_data.values()
        else:
            peers = peers_data
        
        for peer in peers:
            try:
                device = self._parse_peer(peer)
                if device:
                    devices.append(device)
            except (KeyError, TypeError) as e:
                self._logger.warning(f"Failed to parse peer: {e}")
                continue
        
        self._devices = devices
        return devices
    
    def _parse_peer(self, peer: dict[str, Any]) -> Device | None:
        """Parse a single peer from tailscale status.
        
        Args:
            peer: Peer data from tailscale status
            
        Returns:
            Device instance or None if parsing fails
        """
        # Get IP addresses - try multiple possible keys, handle both list and string
        ip_data = (
            peer.get("TailscaleIPs") or 
            peer.get("PrimaryIPs") or 
            peer.get("primary_ips") or 
            peer.get("IPs") or 
            peer.get("ips") or 
            peer.get("IP") or 
            peer.get("ip")
        )
        
        if not ip_data:
            return None
        
        if isinstance(ip_data, list):
            ip = ip_data[0] if ip_data else None
        else:
            ip = ip_data
            
        if not ip:
            return None
        
        # Skip if this is ourself
        if ip == self._self_ip:
            return None
        
        # Get device name and hostname - try multiple possible keys
        name = peer.get("Name") or peer.get("name") or peer.get("HostName") or peer.get("hostname") or "unknown"
        hostname = peer.get("HostName") or peer.get("hostname") or name
        
        # Remove .tailnet.com suffix from name if present
        if "." in name:
            name = name.split(".")[0]
        
        # Check online status - try multiple possible keys
        online = peer.get("Online") if "Online" in peer else peer.get("online", False)
        
        # Get last seen time - try multiple possible keys
        last_seen = peer.get("LastSeen") or peer.get("last_seen")
        
        # Get machine ID - try multiple possible keys
        machine_id = peer.get("ID") or peer.get("id", "")
        
        return Device(
            name=name,
            hostname=hostname,
            ip=ip,
            online=online,
            last_seen=last_seen,
            machine_id=machine_id,
        )
    
    def get_devices(self) -> list[Device]:
        """Get current list of discovered devices.
        
        Returns:
            List of devices (may be empty if not discovered yet)
        """
        return self._devices.copy()
    
    def get_online_devices(self) -> list[Device]:
        """Get only online devices.
        
        Returns:
            List of online devices
        """
        return [d for d in self._devices if d.online]
    
    def get_device_by_ip(self, ip: str) -> Device | None:
        """Get device by IP address.
        
        Args:
            ip: Tailscale IP address
            
        Returns:
            Device if found, None otherwise
        """
        for device in self._devices:
            if device.ip == ip:
                return device
        return None
    
    def get_device_by_name(self, name: str) -> Device | None:
        """Get device by name.
        
        Args:
            name: Device name to search for
            
        Returns:
            Device if found, None otherwise
        """
        name_lower = name.lower()
        for device in self._devices:
            if device.name.lower() == name_lower:
                return device
        return None
    
def discover_devices() -> list[Device]:
    """Convenience function to discover devices.
    
    Returns:
        List of discovered devices
        
    Raises:
        DeviceDiscoveryError: If discovery fails
    """
    discovery = DeviceDiscovery()
    return discovery.discover()
