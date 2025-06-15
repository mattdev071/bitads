import json
import logging
import uuid
import ssl
from contextlib import asynccontextmanager
from typing import Annotated, Optional, List

import uvicorn
from fastapi import (
    FastAPI,
    Request,
    Depends,
    Header,
    status,
    Path,
)
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from common import dependencies as common_dependencies, utils
from common.environ import Environ as CommonEnviron
from common.helpers import const
from common.miner import dependencies
from common.miner.environ import Environ
from common.miner.schemas import VisitorSchema
from common.schemas.bitads import CampaignStatus
from common.schemas.campaign import CampaignType
from common.services.geoip.base import GeoIpService
from proxies.apis.fetch_from_db_test import router as test_router
from proxies.apis.get_database import router as database_router
from proxies.apis.logging import router as logs_router
from proxies.apis.two_factor import router as two_factor_router
from proxies.apis.version import router as version_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)

# Initialize services
database_manager = common_dependencies.get_database_manager(
    "miner", CommonEnviron.SUBTENSOR_NETWORK
)
campaign_service = common_dependencies.get_campaign_service(database_manager)
miner_service = dependencies.get_miner_service(database_manager)
two_factor_service = common_dependencies.get_two_factor_service(
    database_manager
)

# SSL Context configuration
def create_ssl_context():
    try:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain("cert.pem", "key.pem")
        return ssl_context
    except Exception as e:
        log.error(f"Error creating SSL context: {str(e)}")
        return None

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        app.state.database_manager = database_manager
        app.state.two_factor_service = two_factor_service
        app.state.campaign_service = campaign_service
        app.state.ssl_context = create_ssl_context()
        yield
    except Exception as e:
        log.error(f"Error in lifespan: {str(e)}")
        raise

app = FastAPI(
    version="0.8.7",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount(
    "/statics", StaticFiles(directory="statics", html=True), name="statics"
)

# Include routers
app.include_router(version_router)
app.include_router(logs_router)
app.include_router(two_factor_router)

@app.exception_handler(status.HTTP_500_INTERNAL_SERVER_ERROR)
async def internal_exception_handler(request: Request, exc: Exception):
    log.error(f"Internal server error: {str(exc)}", exc_info=True)
    return RedirectResponse("/statics/500.html", status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

@app.exception_handler(status.HTTP_404_NOT_FOUND)
async def not_found_exception_handler(request: Request, exc: Exception):
    log.warning(f"Not found: {request.url}")
    return RedirectResponse("/statics/404.html", status_code=status.HTTP_404_NOT_FOUND)

@app.get("/visitors/by_campaign_item")
async def get_visits_by_campaign_item(
    campaign_item: str,
) -> List[VisitorSchema]:
    try:
        return await miner_service.get_visits_by_campaign_item(campaign_item)
    except Exception as e:
        log.error(f"Error getting visits by campaign item: {str(e)}")
        return []

@app.get("/visitors/by_ip")
async def get_visits_by_ip(
    ip_address: str,
) -> List[VisitorSchema]:
    try:
        return await miner_service.get_by_ip_address(ip_address)
    except Exception as e:
        log.error(f"Error getting visits by IP: {str(e)}")
        return []

@app.get("/visitors/{id}")
async def get_visit_by_id(id: str) -> Optional[VisitorSchema]:
    try:
        return await miner_service.get_visit_by_id(id)
    except Exception as e:
        log.error(f"Error getting visit by ID: {str(e)}")
        return None

@app.get("/{campaign_id}/{campaign_item}")
async def fetch_request_data_and_redirect(
    campaign_id: str,
    campaign_item: Annotated[
        str,
        Path(
            pattern=r"^[a-zA-Z0-9]{13}$",  # Updated from regex to pattern
            title="Campaign Item",
            description="Must be exactly 13 alphanumeric characters",
        ),
    ],
    request: Request,
    geoip_service: Annotated[
        GeoIpService, Depends(common_dependencies.get_geo_ip_service)
    ],
    user_agent: Annotated[str, Header()],
    referer: Annotated[Optional[str], Header()] = None,
):
    try:
        campaign = await campaign_service.get_campaign_by_id(campaign_id)
        if not campaign or campaign.status != CampaignStatus.ACTIVATED:
            log.warning(f"Campaign {campaign_id} not found or not activated")
            return RedirectResponse("/statics/404.html")

        # Get client IP
        ip = request.headers.get("X-Forwarded-For", request.client.host)
        
        # Check country restrictions
        ipaddr_info = geoip_service.get_ip_info(ip)
        if ipaddr_info and ipaddr_info.country_code not in json.loads(
            campaign.countries_approved_for_product_sales
        ):
            log.warning(f"Access denied for country: {ipaddr_info.country_code}")
            return RedirectResponse("/statics/403.html")

        # Get miner info
        hotkey, block = await miner_service.get_hotkey_and_block()
        
        # Create visitor record
        id_ = str(uuid.uuid4())
        visitor = VisitorSchema(
            id=id_,
            referer=referer,
            ip_address=ip,
            campaign_id=campaign_id,
            user_agent=user_agent,
            campaign_item=campaign_item,
            miner_hotkey=hotkey,
            miner_block=block,
            at=False,
            device=utils.determine_device(user_agent),
            country=ipaddr_info.country_name if ipaddr_info else None,
            country_code=ipaddr_info.country_code if ipaddr_info else None,
        )

        # Save visit if not a test redirect
        if const.TEST_REDIRECT != campaign_item:
            await miner_service.add_visit(visitor)
            log.info(f"Saved visit: {visitor.id}")

        # Generate redirect URL
        url = (
            f"{campaign.product_link}?visit_hash={id_}"
            if campaign.type == CampaignType.CPA
            else f"https://v.bitads.ai/campaigns/{campaign_id}?id={id_}"
        )
        
        return RedirectResponse(url=url)
    except Exception as e:
        log.error(f"Error in fetch_request_data_and_redirect: {str(e)}")
        return RedirectResponse("/statics/500.html")

if __name__ == "__main__":
    try:
        ssl_context = create_ssl_context()
        if not ssl_context:
            log.error("Failed to create SSL context. Starting without SSL.")
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=Environ.PROXY_PORT,
            )
        else:
            uvicorn.run(
                app,
                host="0.0.0.0",
                port=Environ.PROXY_PORT,
                ssl_context=ssl_context,
            )
    except Exception as e:
        log.error(f"Error starting server: {str(e)}")
