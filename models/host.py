from dataclasses import dataclass, field


@dataclass
class DockerStack:
    name: str
    path: str
    compose_file: str
    status: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "compose_file": self.compose_file,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DockerStack":
        return cls(**d)


@dataclass
class Host:
    hostname: str
    username: str
    port: int = 22
    label: str = ""
    key_path: str = ""
    os_info: str = ""
    os_pretty: str = ""
    stacks: list[DockerStack] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.label if self.label else self.hostname

    def to_dict(self) -> dict:
        return {
            "hostname": self.hostname,
            "username": self.username,
            "port": self.port,
            "label": self.label,
            "key_path": self.key_path,
            "os_info": self.os_info,
            "os_pretty": self.os_pretty,
            "stacks": [s.to_dict() for s in self.stacks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Host":
        stacks = [DockerStack.from_dict(s) for s in d.get("stacks", [])]
        return cls(
            hostname=d["hostname"],
            username=d["username"],
            port=d.get("port", 22),
            label=d.get("label", ""),
            key_path=d.get("key_path", ""),
            os_info=d.get("os_info", ""),
            os_pretty=d.get("os_pretty", ""),
            stacks=stacks,
        )
