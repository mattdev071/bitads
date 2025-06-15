import logging
import operator
from datetime import datetime
from typing import List, Set, Dict, Tuple, Optional, Any

from common import converters
from common.db.database import DatabaseManager
from common.db.repositories import bitads_data
from common.miner.schemas import VisitorSchema
from common.schemas.bitads import BitAdsDataSchema
from common.schemas.completed_visit import CompletedVisitSchema
from common.schemas.paged import PaginationInfo
from common.schemas.sales import SalesStatus, OrderQueueSchema, OrderQueueStatus
from common.schemas.shopify import SaleData
from common.services.bitads.base import BitAdsService
from common.validator.schemas import ValidatorTrackingData

log = logging.getLogger(__name__)


class BitAdsServiceImpl(BitAdsService):
    def __init__(self, database_manager: DatabaseManager, ndigits: int = 5):
        self.database_manager = database_manager
        self.ndigits = ndigits

    async def add_or_update_validator_bitads_data(
        self, validator_data: ValidatorTrackingData, sale_data: SaleData
    ):
        with self.database_manager.get_session("active") as session:
            data = validator_data.model_dump() | converters.to_bitads_extra_data(
                sale_data
            )
            bitads_data.add_or_update(
                session,
                BitAdsDataSchema(
                    **data,
                    country_code=sale_data.order_details.customer_info.address.country_code,
                ),
            )

    async def get_bitads_data_between(
        self,
        updated_from: datetime = None,
        updated_to: datetime = None,
        page_number: int = 1,
        page_size: int = 500,
    ) -> List[BitAdsDataSchema]:
        limit = page_size
        offset = (page_number - 1) * page_size
        with self.database_manager.get_session("active") as session:
            return bitads_data.get_data_between(
                session, updated_from, updated_to, limit, offset
            )

    async def get_bitads_data_between_paged(
        self,
        updated_from: datetime = None,
        updated_to: datetime = None,
        page_number: int = 1,
        page_size: int = 500,
    ) -> Dict[str, Any]:
        limit = page_size
        offset = (page_number - 1) * page_size
        with self.database_manager.get_session("active") as session:
            data, total = bitads_data.get_data_between_paged(
                session, updated_from, updated_to, limit, offset
            )
            return dict(
                data=data,
                pagination=PaginationInfo(
                    total=total,
                    page_size=page_size,
                    page_number=page_number,
                    next_page_number=page_number + 1,
                ),
            )

    async def get_last_update_bitads_data(self, exclude_hotkey: str):
        with self.database_manager.get_session("active") as session:
            return bitads_data.get_max_date_excluding_hotkey(session, exclude_hotkey)

    async def add_by_visits(self, visits: Set[VisitorSchema]) -> None:
        with self.database_manager.get_session("active") as session:
            existed_ids = bitads_data.filter_existing_ids(
                session, set(map(operator.attrgetter("id"), visits))
            )
            for visit in visits:
                if visit.id in existed_ids:
                    continue
                bitads_data.add_data(session, BitAdsDataSchema(**visit.model_dump()))

    async def add_by_visit(self, visit: VisitorSchema) -> None:
        with self.database_manager.get_session("active") as session:
            bitads_data.add_or_update(session, BitAdsDataSchema(**visit.model_dump()))

    async def add_bitads_data(self, datas: Set[BitAdsDataSchema]) -> None:
        with self.database_manager.get_session("active") as session:
            for data in datas:
                bitads_data.add_or_update(session, data)

    async def get_data_by_ids(self, ids: Set[str]) -> Set[BitAdsDataSchema]:
        result = set()
        with self.database_manager.get_session("active") as session:
            for id_ in ids:
                data = bitads_data.get_data(session, id_)
                if data:
                    result.add(data)
        return result

    async def add_completed_visits(self, visits: List[CompletedVisitSchema]):
        datas = {
            BitAdsDataSchema(
                **v.model_dump(exclude={"complete_block"}),
                sales_status=SalesStatus.COMPLETED,
            )
            for v in visits
        }
        await self.add_bitads_data(datas)

    async def update_sale_status_if_needed(
        self, campaign_id: str, sale_to: datetime
    ) -> None:
        log.debug(f"Completing sales with date less than: {sale_to}")
        with self.database_manager.get_session("active") as session:
            bitads_data.complete_sales_less_than_date(session, campaign_id, sale_to)

    async def add_by_queue_items(
        self, validator_block: int, validator_hotkey: str, items: List[OrderQueueSchema]
    ) -> Dict[str, Tuple[OrderQueueStatus, Optional[BitAdsDataSchema]]]:
        result = {}
        with self.database_manager.get_session("active") as session:
            for item in items:
                existed_data = bitads_data.get_data(session, item.id)
                if not existed_data:
                    result[item.id] = OrderQueueStatus.VISIT_NOT_FOUND, None
                    continue
                sale_amount = float(item.order_info.totalAmount)
                refund_amount = (
                    float(item.refund_info.totalAmount) if item.refund_info else 0.0
                )
                sale_amount -= refund_amount
                sales = len(item.order_info.items)
                refund = len(item.refund_info.items) if item.refund_info else 0

                extra_fields = dict(sales_status=SalesStatus.COMPLETED) if refund else dict()

                new_data = existed_data.model_copy(
                    update=dict(
                        sale_date=item.order_info.sale_date,
                        order_info=item.order_info,
                        refund_info=item.refund_info,
                        validator_block=validator_block,
                        validator_hotkey=validator_hotkey,
                        sales=sales,
                        sale_amount=sale_amount,
                        refund=refund,
                        **extra_fields
                    )
                )
                try:
                    new_data = bitads_data.add_or_update(session, new_data)
                    result[item.id] = OrderQueueStatus.PROCESSED, new_data
                except Exception:
                    log.exception(f"Add BitAds data exception on id: {item.id}")
                    result[item.id] = OrderQueueStatus.ERROR, None
        return result

    async def get_by_campaign_items(
        self,
        campaign_items: List[str],
        page_number: int = 1,
        page_size: int = 500,
    ) -> List[BitAdsDataSchema]:
        limit = page_size
        offset = (page_number - 1) * page_size
        with self.database_manager.get_session("active") as session:
            return bitads_data.get_bitads_data_by_campaign_items(
                session, campaign_items, limit, offset
            )
