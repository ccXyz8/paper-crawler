from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..http_client import HttpClient
from ..models import ConferenceConfig, Paper


class ConferenceAdapter(ABC):
    @abstractmethod
    def iter_papers(
        self, conference: ConferenceConfig, client: HttpClient
    ) -> Iterator[Paper]:
        raise NotImplementedError
