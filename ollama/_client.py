import os
import io
import json
import httpx
from os import PathLike
from pathlib import Path
from hashlib import sha256
from base64 import b64encode

from typing import Any, AnyStr, Union, Optional, Sequence, Mapping

import sys

if sys.version_info < (3, 9):
  from typing import Iterator, AsyncIterator
else:
  from collections.abc import Iterator, AsyncIterator

from ollama._types import Message, Options


class BaseClient:
  def __init__(self, client, base_url: Optional[str] = None) -> None:
    base_url = base_url or os.getenv('OLLAMA_HOST', 'http://127.0.0.1:11434')
    self._client = client(base_url=base_url, follow_redirects=True, timeout=None)


class Client(BaseClient):
  def __init__(self, base_url: Optional[str] = None) -> None:
    super().__init__(httpx.Client, base_url)

  def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
    response = self._client.request(method, url, **kwargs)
    response.raise_for_status()
    return response

  def _stream(self, method: str, url: str, **kwargs) -> Iterator[Mapping[str, Any]]:
    with self._client.stream(method, url, **kwargs) as r:
      for line in r.iter_lines():
        partial = json.loads(line)
        if e := partial.get('error'):
          raise Exception(e)
        yield partial

  def _request_stream(
    self,
    *args,
    stream: bool = False,
    **kwargs,
  ) -> Union[Mapping[str, Any], Iterator[Mapping[str, Any]]]:
    return self._stream(*args, **kwargs) if stream else self._request(*args, **kwargs).json()

  def generate(
    self,
    model: str = '',
    prompt: str = '',
    system: str = '',
    template: str = '',
    context: Optional[Sequence[int]] = None,
    stream: bool = False,
    raw: bool = False,
    format: str = '',
    images: Optional[Sequence[AnyStr]] = None,
    options: Optional[Options] = None,
  ) -> Union[Mapping[str, Any], Iterator[Mapping[str, Any]]]:
    """
    Returns `GenerateResponse` if `stream` is `False`, otherwise returns a `GenerateResponse` generator.
    """

    if not model:
      raise Exception('must provide a model')

    return self._request_stream(
      'POST',
      '/api/generate',
      json={
        'model': model,
        'prompt': prompt,
        'system': system,
        'template': template,
        'context': context or [],
        'stream': stream,
        'raw': raw,
        'images': [_encode_image(image) for image in images or []],
        'format': format,
        'options': options or {},
      },
      stream=stream,
    )

  def chat(
    self,
    model: str = '',
    messages: Optional[Sequence[Message]] = None,
    stream: bool = False,
    format: str = '',
    options: Optional[Options] = None,
  ) -> Union[Mapping[str, Any], Iterator[Mapping[str, Any]]]:
    """
    Returns `ChatResponse` if `stream` is `False`, otherwise returns a `ChatResponse` generator.
    """

    if not model:
      raise Exception('must provide a model')

    for message in messages or []:
      if not isinstance(message, dict):
        raise TypeError('messages must be a list of strings')
      if not (role := message.get('role')) or role not in ['system', 'user', 'assistant']:
        raise Exception('messages must contain a role and it must be one of "system", "user", or "assistant"')
      if not message.get('content'):
        raise Exception('messages must contain content')
      if images := message.get('images'):
        message['images'] = [_encode_image(image) for image in images]

    return self._request_stream(
      'POST',
      '/api/chat',
      json={
        'model': model,
        'messages': messages,
        'stream': stream,
        'format': format,
        'options': options or {},
      },
      stream=stream,
    )

  def pull(
    self,
    model: str,
    insecure: bool = False,
    stream: bool = False,
  ) -> Union[Mapping[str, Any], Iterator[Mapping[str, Any]]]:
    """
    Returns `ProgressResponse` if `stream` is `False`, otherwise returns a `ProgressResponse` generator.
    """
    return self._request_stream(
      'POST',
      '/api/pull',
      json={
        'name': model,
        'insecure': insecure,
        'stream': stream,
      },
      stream=stream,
    )

  def push(
    self,
    model: str,
    insecure: bool = False,
    stream: bool = False,
  ) -> Union[Mapping[str, Any], Iterator[Mapping[str, Any]]]:
    """
    Returns `ProgressResponse` if `stream` is `False`, otherwise returns a `ProgressResponse` generator.
    """
    return self._request_stream(
      'POST',
      '/api/push',
      json={
        'name': model,
        'insecure': insecure,
        'stream': stream,
      },
      stream=stream,
    )

  def create(
    self,
    model: str,
    path: Optional[Union[str, PathLike]] = None,
    modelfile: Optional[str] = None,
    stream: bool = False,
  ) -> Union[Mapping[str, Any], Iterator[Mapping[str, Any]]]:
    """
    Returns `ProgressResponse` if `stream` is `False`, otherwise returns a `ProgressResponse` generator.
    """
    if (realpath := _as_path(path)) and realpath.exists():
      modelfile = self._parse_modelfile(realpath.read_text(), base=realpath.parent)
    elif modelfile:
      modelfile = self._parse_modelfile(modelfile)
    else:
      raise Exception('must provide either path or modelfile')

    return self._request_stream(
      'POST',
      '/api/create',
      json={
        'name': model,
        'modelfile': modelfile,
        'stream': stream,
      },
      stream=stream,
    )

  def _parse_modelfile(self, modelfile: str, base: Optional[Path] = None) -> str:
    base = Path.cwd() if base is None else base

    out = io.StringIO()
    for line in io.StringIO(modelfile):
      command, _, args = line.partition(' ')
      if command.upper() in ['FROM', 'ADAPTER']:
        path = Path(args).expanduser()
        path = path if path.is_absolute() else base / path
        if path.exists():
          args = f'@{self._create_blob(path)}'

      print(command, args, file=out)
    return out.getvalue()

  def _create_blob(self, path: Union[str, Path]) -> str:
    sha256sum = sha256()
    with open(path, 'rb') as r:
      while True:
        chunk = r.read(32 * 1024)
        if not chunk:
          break
        sha256sum.update(chunk)

    digest = f'sha256:{sha256sum.hexdigest()}'

    try:
      self._request('HEAD', f'/api/blobs/{digest}')
    except httpx.HTTPStatusError as e:
      if e.response.status_code != 404:
        raise

      with open(path, 'rb') as r:
        self._request('PUT', f'/api/blobs/{digest}', content=r)

    return digest

  def delete(self, model: str) -> Mapping[str, Any]:
    response = self._request('DELETE', '/api/delete', json={'name': model})
    return {'status': 'success' if response.status_code == 200 else 'error'}

  def list(self) -> Mapping[str, Any]:
    return self._request('GET', '/api/tags').json().get('models', [])

  def copy(self, source: str, target: str) -> Mapping[str, Any]:
    response = self._request('POST', '/api/copy', json={'source': source, 'destination': target})
    return {'status': 'success' if response.status_code == 200 else 'error'}

  def show(self, model: str) -> Mapping[str, Any]:
    return self._request('GET', '/api/show', json={'name': model}).json()


class AsyncClient(BaseClient):
  def __init__(self, base_url: Optional[str] = None) -> None:
    super().__init__(httpx.AsyncClient, base_url)

  async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
    response = await self._client.request(method, url, **kwargs)
    response.raise_for_status()
    return response

  async def _stream(self, method: str, url: str, **kwargs) -> AsyncIterator[Mapping[str, Any]]:
    async def inner():
      async with self._client.stream(method, url, **kwargs) as r:
        async for line in r.aiter_lines():
          partial = json.loads(line)
          if e := partial.get('error'):
            raise Exception(e)
          yield partial

    return inner()

  async def _request_stream(
    self,
    *args,
    stream: bool = False,
    **kwargs,
  ) -> Union[Mapping[str, Any], AsyncIterator[Mapping[str, Any]]]:
    if stream:
      return await self._stream(*args, **kwargs)

    response = await self._request(*args, **kwargs)
    return response.json()

  async def generate(
    self,
    model: str = '',
    prompt: str = '',
    system: str = '',
    template: str = '',
    context: Optional[Sequence[int]] = None,
    stream: bool = False,
    raw: bool = False,
    format: str = '',
    images: Optional[Sequence[AnyStr]] = None,
    options: Optional[Options] = None,
  ) -> Union[Mapping[str, Any], AsyncIterator[Mapping[str, Any]]]:
    """
    Returns `GenerateResponse` if `stream` is `False`, otherwise returns an asynchronous `GenerateResponse` generator.
    """
    if not model:
      raise Exception('must provide a model')

    return await self._request_stream(
      'POST',
      '/api/generate',
      json={
        'model': model,
        'prompt': prompt,
        'system': system,
        'template': template,
        'context': context or [],
        'stream': stream,
        'raw': raw,
        'images': [_encode_image(image) for image in images or []],
        'format': format,
        'options': options or {},
      },
      stream=stream,
    )

  async def chat(
    self,
    model: str = '',
    messages: Optional[Sequence[Message]] = None,
    stream: bool = False,
    format: str = '',
    options: Optional[Options] = None,
  ) -> Union[Mapping[str, Any], AsyncIterator[Mapping[str, Any]]]:
    """
    Returns `ChatResponse` if `stream` is `False`, otherwise returns an asynchronous `ChatResponse` generator.
    """
    if not model:
      raise Exception('must provide a model')

    for message in messages or []:
      if not isinstance(message, dict):
        raise TypeError('messages must be a list of strings')
      if not (role := message.get('role')) or role not in ['system', 'user', 'assistant']:
        raise Exception('messages must contain a role and it must be one of "system", "user", or "assistant"')
      if not message.get('content'):
        raise Exception('messages must contain content')
      if images := message.get('images'):
        message['images'] = [_encode_image(image) for image in images]

    return await self._request_stream(
      'POST',
      '/api/chat',
      json={
        'model': model,
        'messages': messages,
        'stream': stream,
        'format': format,
        'options': options or {},
      },
      stream=stream,
    )

  async def pull(
    self,
    model: str,
    insecure: bool = False,
    stream: bool = False,
  ) -> Union[Mapping[str, Any], AsyncIterator[Mapping[str, Any]]]:
    """
    Returns `ProgressResponse` if `stream` is `False`, otherwise returns a `ProgressResponse` generator.
    """
    return await self._request_stream(
      'POST',
      '/api/pull',
      json={
        'name': model,
        'insecure': insecure,
        'stream': stream,
      },
      stream=stream,
    )

  async def push(
    self,
    model: str,
    insecure: bool = False,
    stream: bool = False,
  ) -> Union[Mapping[str, Any], AsyncIterator[Mapping[str, Any]]]:
    """
    Returns `ProgressResponse` if `stream` is `False`, otherwise returns a `ProgressResponse` generator.
    """
    return await self._request_stream(
      'POST',
      '/api/push',
      json={
        'name': model,
        'insecure': insecure,
        'stream': stream,
      },
      stream=stream,
    )

  async def create(
    self,
    model: str,
    path: Optional[Union[str, PathLike]] = None,
    modelfile: Optional[str] = None,
    stream: bool = False,
  ) -> Union[Mapping[str, Any], AsyncIterator[Mapping[str, Any]]]:
    """
    Returns `ProgressResponse` if `stream` is `False`, otherwise returns a `ProgressResponse` generator.
    """
    if (realpath := _as_path(path)) and realpath.exists():
      modelfile = await self._parse_modelfile(realpath.read_text(), base=realpath.parent)
    elif modelfile:
      modelfile = await self._parse_modelfile(modelfile)
    else:
      raise Exception('must provide either path or modelfile')

    return await self._request_stream(
      'POST',
      '/api/create',
      json={
        'name': model,
        'modelfile': modelfile,
        'stream': stream,
      },
      stream=stream,
    )

  async def _parse_modelfile(self, modelfile: str, base: Optional[Path] = None) -> str:
    base = Path.cwd() if base is None else base

    out = io.StringIO()
    for line in io.StringIO(modelfile):
      command, _, args = line.partition(' ')
      if command.upper() in ['FROM', 'ADAPTER']:
        path = Path(args).expanduser()
        path = path if path.is_absolute() else base / path
        if path.exists():
          args = f'@{await self._create_blob(path)}'

      print(command, args, file=out)
    return out.getvalue()

  async def _create_blob(self, path: Union[str, Path]) -> str:
    sha256sum = sha256()
    with open(path, 'rb') as r:
      while True:
        chunk = r.read(32 * 1024)
        if not chunk:
          break
        sha256sum.update(chunk)

    digest = f'sha256:{sha256sum.hexdigest()}'

    try:
      await self._request('HEAD', f'/api/blobs/{digest}')
    except httpx.HTTPStatusError as e:
      if e.response.status_code != 404:
        raise

      async def upload_bytes():
        with open(path, 'rb') as r:
          while True:
            chunk = r.read(32 * 1024)
            if not chunk:
              break
            yield chunk

      await self._request('PUT', f'/api/blobs/{digest}', content=upload_bytes())

    return digest

  async def delete(self, model: str) -> Mapping[str, Any]:
    response = await self._request('DELETE', '/api/delete', json={'name': model})
    return {'status': 'success' if response.status_code == 200 else 'error'}

  async def list(self) -> Mapping[str, Any]:
    response = await self._request('GET', '/api/tags')
    return response.json().get('models', [])

  async def copy(self, source: str, target: str) -> Mapping[str, Any]:
    response = await self._request('POST', '/api/copy', json={'source': source, 'destination': target})
    return {'status': 'success' if response.status_code == 200 else 'error'}

  async def show(self, model: str) -> Mapping[str, Any]:
    response = await self._request('GET', '/api/show', json={'name': model})
    return response.json()


def _encode_image(image) -> str:
  if p := _as_path(image):
    b64 = b64encode(p.read_bytes())
  elif b := _as_bytesio(image):
    b64 = b64encode(b.read())
  else:
    raise Exception('images must be a list of bytes, path-like objects, or file-like objects')

  return b64.decode('utf-8')


def _as_path(s: Optional[Union[str, PathLike]]) -> Union[Path, None]:
  if isinstance(s, str) or isinstance(s, Path):
    return Path(s)
  return None


def _as_bytesio(s: Any) -> Union[io.BytesIO, None]:
  if isinstance(s, io.BytesIO):
    return s
  elif isinstance(s, bytes):
    return io.BytesIO(s)
  return None
