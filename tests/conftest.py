from pathlib import Path

import pytest

from tests.support.media import make_media
from tests.support.faces import make_face_media
from tests.support.render import make_render_media
from tests.support.server import MediaServer


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    return make_media(tmp_path_factory.mktemp("media"))


@pytest.fixture(scope="session")
def render_media(tmp_path_factory) -> dict[str, Path]:
    return make_render_media(tmp_path_factory.mktemp("render-media"))


@pytest.fixture(scope="session")
def face_media(tmp_path_factory) -> dict[str, Path]:
    return make_face_media(tmp_path_factory.mktemp("face-media"))


@pytest.fixture(scope="session")
def media_server(media):
    server = MediaServer(media["sample.mp4"].parent)
    yield server
    server.close()
