"""Utility functions for LXC container management and monitoring."""

import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

try:
    import paramiko
except ImportError:
    logging.error("Paramiko package not installed. SSH functionality disabled.")

from config import (BACKUP_DIR, DEFAULTS, IGNORE_LXC, LOG_FILE, LXC_TIER_ASSOCIATIONS,
                   PROXMOX_HOSTNAME, config)

lock = Lock()


def run_command(cmd, timeout=30):
    """Execute a command locally or remotely based on configuration."""
    use_remote_proxmox = config.get('DEFAULT', {}).get('use_remote_proxmox', False)
    logging.debug("Inside run_command: use_remote_proxmox = %s", use_remote_proxmox)
    return (run_remote_command if use_remote_proxmox else run_local_command)(cmd, timeout)


def run_local_command(cmd, timeout=30):
    """Execute a command locally with timeout."""
    try:
        result = subprocess.check_output(
            cmd, shell=True, timeout=timeout, stderr=subprocess.STDOUT
        ).decode('utf-8').strip()
        logging.debug("Command '%s' executed successfully. Output: %s", cmd, result)
        return result
    except subprocess.TimeoutExpired:
        logging.error("Command '%s' timed out after %d seconds", cmd, timeout)
    except subprocess.CalledProcessError as e:
        logging.error("Command '%s' failed: %s", cmd, e.output.decode('utf-8'))
    except Exception as e:  # pylint: disable=broad-except
        logging.error("Unexpected error executing '%s': %s", cmd, str(e))
    return None


def run_remote_command(cmd, timeout=30):
    """Execute a command on remote Proxmox host via SSH."""
    logging.debug("Running remote command: %s", cmd)
    ssh = None
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=config.get('DEFAULT', {}).get('proxmox_host'),
            port=config.get('DEFAULT', {}).get('ssh_port', 22),
            username=config.get('DEFAULT', {}).get('ssh_user'),
            password=config.get('DEFAULT', {}).get('ssh_password'),
            key_filename=config.get('DEFAULT', {}).get('ssh_key_path')
        )
        _, stdout, _ = ssh.exec_command(cmd, timeout=timeout)
        output = stdout.read().decode('utf-8').strip()
        logging.debug("Remote command '%s' executed successfully: %s", cmd, output)
        return output
    except paramiko.SSHException as e:
        logging.error("SSH execution failed: %s", str(e))
    except Exception as e:  # pylint: disable=broad-except
        logging.error("Unexpected SSH error executing '%s': %s", cmd, str(e))
    finally:
        if ssh:
            ssh.close()
    return None


def get_containers():
    """Return list of container IDs, excluding ignored ones."""
    containers = run_command("pct list | awk 'NR>1 {print $1}'")
    return [ctid for ctid in containers.splitlines() if ctid not in IGNORE_LXC]


def is_container_running(ctid):
    """Check if container is running."""
    status = run_command(f"pct status {ctid}")
    return status and "status: running" in status.lower()


def backup_container_settings(ctid, settings):
    """Backup container configuration to JSON file."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        backup_file = os.path.join(BACKUP_DIR, f"{ctid}_backup.json")
        with lock:
            with open(backup_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f)
        logging.debug("Backup saved for container %s: %s", ctid, settings)
    except Exception as e:  # pylint: disable=broad-except
        logging.error("Failed to backup settings for %s: %s", ctid, str(e))


def load_backup_settings(ctid):
    """Load container configuration from backup JSON file."""
    try:
        backup_file = os.path.join(BACKUP_DIR, f"{ctid}_backup.json")
        if os.path.exists(backup_file):
            with lock:
                with open(backup_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
            logging.debug("Loaded backup for container %s: %s", ctid, settings)
            return settings
        logging.warning("No backup found for container %s", ctid)
        return None
    except Exception as e:  # pylint: disable=broad-except
        logging.error("Failed to load backup for %s: %s", ctid, str(e))
        return None


def rollback_container_settings(ctid):
    """Restore container settings from backup."""
    settings = load_backup_settings(ctid)
    if settings:
        logging.info("Rolling back container %s to backup settings", ctid)
        run_command(f"pct set {ctid} -cores {settings['cores']}")
        run_command(f"pct set {ctid} -memory {settings['memory']}")


def log_json_event(ctid, action, resource_change):
    """Log container change events in JSON format."""
    log_data = {
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        "proxmox_host": PROXMOX_HOSTNAME,
        "container_id": ctid,
        "action": action,
        "change": resource_change
    }
    with lock:
        with open(LOG_FILE.replace('.log', '.json'), 'a', encoding='utf-8') as json_log_file:
            json_log_file.write(json.dumps(log_data) + '\n')


def get_total_cores():
    """Calculate available CPU cores after reserving percentage."""
    total_cores = int(run_command("nproc"))
    reserved_cores = max(1, int(total_cores * DEFAULTS['reserve_cpu_percent'] / 100))
    available_cores = total_cores - reserved_cores
    logging.debug(
        "Total cores: %d, Reserved: %d, Available: %d",
        total_cores, reserved_cores, available_cores
    )
    return available_cores


def get_total_memory():
    """Calculate available memory after reserving fixed amount."""
    try:
        command_output = run_command("free -m | awk '/^Mem:/ {print $2}'")
        total_memory = int(command_output.strip()) if command_output else 0
    except (ValueError, subprocess.CalledProcessError) as e:
        logging.error("Failed to get total memory: %s", str(e))
        total_memory = 0

    available_memory = max(0, total_memory - DEFAULTS['reserve_memory_mb'])
    logging.debug(
        "Total memory: %dMB, Reserved: %dMB, Available: %dMB",
        total_memory, DEFAULTS['reserve_memory_mb'], available_memory
    )
    return available_memory


def get_cpu_usage(ctid):
    """Get container CPU usage using multiple fallback methods."""
    def run_cmd(command):
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, check=True
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError as e:
            logging.warning("Command failed: %s, Error: %s", command, str(e))
            return ""

    def loadavg_method(ctid):
        try:
            loadavg = float(run_cmd(f"pct exec {ctid} -- cat /proc/loadavg").split()[0])
            num_cpus = int(run_cmd(f"pct exec {ctid} -- nproc"))
            if num_cpus == 0:
                raise ValueError("Number of CPUs is zero.")
            return round(min((loadavg / num_cpus) * 100, 100.0), 2)
        except Exception as e:  # pylint: disable=broad-except
            raise RuntimeError("Loadavg method failed: %s", str(e)) from e

    def load_method(ctid):
        try:
            cmd = f"pct exec {ctid} -- cat /proc/stat | grep '^cpu '"
            initial_times = list(map(float, run_cmd(cmd).split()[1:]))
            initial_total = sum(initial_times)
            initial_idle = initial_times[3]

            time.sleep(1)

            new_times = list(map(float, run_cmd(cmd).split()[1:]))
            new_total = sum(new_times)
            new_idle = new_times[3]

            total_diff = new_total - initial_total
            idle_diff = new_idle - initial_idle

            if total_diff == 0:
                raise ValueError("Total CPU time did not change.")

            return round(
                max(min(100.0 * (total_diff - idle_diff) / total_diff, 100.0), 0.0), 2
            )
        except Exception as e:  # pylint: disable=broad-except
            raise RuntimeError("Load method failed: %s", str(e)) from e

    methods = [
        ("Load Average", loadavg_method),
        ("Load", load_method),
    ]

    for method_name, method in methods:
        try:
            cpu = method(ctid)
            if cpu is not None and cpu >= 0.0:
                logging.info("CPU usage for %s using %s: %s%%", ctid, method_name, cpu)
                return cpu
        except Exception as e:  # pylint: disable=broad-except
            logging.warning("%s failed for %s: %s", method_name, ctid, str(e))

    logging.error("All CPU usage methods failed for %s. Using 0.0", ctid)
    return 0.0


def get_memory_usage(ctid):
    """Get container memory usage percentage."""
    mem_info = run_command(
        f"pct exec {ctid} -- awk '/MemTotal/ {{t=$2}} /MemAvailable/ {{a=$2}} "
        f"END {{print t, t-a}}' /proc/meminfo"
    )
    if mem_info:
        try:
            total, used = map(int, mem_info.split())
            return (used * 100) / total
        except ValueError:
            logging.error("Failed to parse memory info for %s: '%s'", ctid, mem_info)
    logging.error("Failed to get memory usage for %s", ctid)
    return 0.0


def is_ignored(ctid):
    """Check if container is in ignore list."""
    return str(ctid) in IGNORE_LXC


def get_container_data(ctid):
    """Collect container resource usage data."""
    if is_ignored(ctid) or not is_container_running(ctid):
        return None

    logging.debug("Collecting data for container %s", ctid)
    try:
        cores = int(run_command(f"pct config {ctid} | grep cores | awk '{{print $2}}'"))
        memory = int(run_command(f"pct config {ctid} | grep memory | awk '{{print $2}}'"))
        settings = {"cores": cores, "memory": memory}
        backup_container_settings(ctid, settings)
        return {
            "cpu": get_cpu_usage(ctid),
            "mem": get_memory_usage(ctid),
            "initial_cores": cores,
            "initial_memory": memory,
        }
    except Exception as e:  # pylint: disable=broad-except
        logging.error("Error collecting data for %s: %s", ctid, str(e))
        return None


def collect_container_data():
    """Collect data from all containers in parallel."""
    containers = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_ctid = {
            executor.submit(get_container_data, ctid): ctid 
            for ctid in get_containers()
        }
        for future in as_completed(future_to_ctid):
            ctid = future_to_ctid[future]
            try:
                data = future.result()
                if data:
                    containers[ctid] = data
                    logging.debug("Container %s data: %s", ctid, data)
            except Exception as e:  # pylint: disable=broad-except
                logging.error("Error retrieving data for %s: %s", ctid, str(e))
    return containers


def prioritize_containers(containers):
    """Sort containers by resource usage priority."""
    if not containers:
        logging.info("No containers to prioritize.")
        return []

    try:
        priorities = sorted(
            containers.items(),
            key=lambda item: (item[1]['cpu'], item[1]['mem']),
            reverse=True
        )
        logging.debug("Container priorities: %s", priorities)
        return priorities
    except Exception as e:  # pylint: disable=broad-except
        logging.error("Error prioritizing containers: %s", str(e))
        return []


def get_container_config(ctid):
    """Get container tier configuration."""
    return LXC_TIER_ASSOCIATIONS.get(ctid, DEFAULTS)


def generate_unique_snapshot_name(base_name):
    """Generate timestamped snapshot name."""
    return f"{base_name}-{datetime.now().strftime('%Y%m%d%H%M%S')}"


def generate_cloned_hostname(base_name, clone_number):
    """Generate unique hostname for cloned container."""
    return f"{base_name}-cloned-{clone_number}"
