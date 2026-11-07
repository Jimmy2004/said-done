from pathlib import Path
from .json_source import JsonSource
class MockSource:
    def __init__(self,directory): self.directory=Path(directory)
    def read(self):
        paths=sorted(self.directory.glob('*.json'))
        if not paths: raise ValueError('no mock JSON samples found')
        return [seg for path in paths for seg in JsonSource(path).read()]
