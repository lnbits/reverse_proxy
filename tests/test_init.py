import pytest
from fastapi import APIRouter

from .. import reverse_proxy_ext


# just import router and add it to a test router
@pytest.mark.asyncio
async def test_router():
    router = APIRouter()
    router.include_router(reverse_proxy_ext)
