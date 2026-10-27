"""Stock levels, shared by all orders in this process."""
import asyncio

STOCK = {"A-100": 5, "B-200": 2}


async def reserve(sku, qty):
    """Take qty of sku out of stock; False when there is not enough."""
    if STOCK.get(sku, 0) >= qty:
        await asyncio.sleep(0)  # the warehouse call
        STOCK[sku] -= qty
        return True
    return False


def release(sku, qty):
    # put stock back after a cancled order
    STOCK[sku] = STOCK.get(sku, 0) + qty
