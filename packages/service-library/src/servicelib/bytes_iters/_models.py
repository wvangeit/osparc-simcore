from dataclasses import dataclass

from models_library.bytes_iters import BytesIter, BytesIterCallable, DataSize

from ..progress_bar import ProgressBarData


@dataclass(frozen=True)
class BytesStreamer:
    data_size: DataSize
    bytes_iter_callable: BytesIterCallable

    async def with_progress_bytes_iter(
        self, progress_bar: ProgressBarData
    ) -> BytesIter:
        async for chunk in self.bytes_iter_callable():
            if progress_bar.is_running():
                await progress_bar.update(len(chunk))
            yield chunk
