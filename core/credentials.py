import json
import os
import keyring

_SERVICE_NAME = "DockerStackManager"
_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".docker_stack_manager")
_CONFIG_FILE = os.path.join(_CONFIG_DIR, "hosts.json")


def _ensure_config_dir():
    os.makedirs(_CONFIG_DIR, exist_ok=True)


def save_password(hostname: str, username: str, password: str):
    keyring.set_password(_SERVICE_NAME, f"{username}@{hostname}", password)


def get_password(hostname: str, username: str) -> str | None:
    return keyring.get_password(_SERVICE_NAME, f"{username}@{hostname}")


def delete_password(hostname: str, username: str):
    try:
        keyring.delete_password(_SERVICE_NAME, f"{username}@{hostname}")
    except keyring.errors.PasswordDeleteError:
        pass


def save_hosts(hosts: list) -> None:
    _ensure_config_dir()
    data = [h.to_dict() for h in hosts]
    with open(_CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_hosts() -> list[dict]:
    if not os.path.exists(_CONFIG_FILE):
        return []
    try:
        with open(_CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
