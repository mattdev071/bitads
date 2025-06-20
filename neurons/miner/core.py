# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 bittensor.com
import asyncio
import logging
import time
from datetime import timedelta, datetime
from typing import Type

import bittensor as bt

from common import dependencies as common_dependencies, utils
from common.environ import Environ as CommonEnviron
from common.helpers import const
from common.helpers.logging import log_startup, BittensorLoggingFilter
from common.miner import dependencies
from common.miner.environ import Environ
from common.utils import execute_periodically
from common.validator.environ import Environ as ValidatorEnviron
from neurons.base.operations import BaseOperation
from neurons.miner.operations.notify_order import NotifyOrderOperation
from neurons.miner.operations.ping import PingOperation
from neurons.miner.operations.recent_activity import RecentActivityOperation
from neurons.miner.operations.sync_visits import SyncVisitsOperation
from neurons.protocol import SyncVisits

# import base miner class which takes care of most of the boilerplate
from template.base.miner import BaseMinerNeuron
from template.mock import MockDendrite
from template.validator.forward import forward_each_axon


# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


# Bittensor Miner Template:


class CoreMiner(BaseMinerNeuron):
    def __init__(self, config=None):
        super(CoreMiner, self).__init__(config=config)
        self.loop = asyncio.get_event_loop()

        # Initialize BitAds client
        self.bit_ads_client = common_dependencies.create_bitads_client(
            self.wallet, self.config.bitads.url, self.neuron_type
        )

        # Initialize services
        self.validators = CommonEnviron.VALIDATORS
        self.miners = CommonEnviron.MINERS

        self.database_manager = common_dependencies.get_database_manager(
            self.neuron_type, self.subtensor.network
        )
        self.miner_service = dependencies.get_miner_service(self.database_manager)
        self.campaign_service = common_dependencies.get_campaign_service(
            self.database_manager
        )
        self.recent_activity_service = dependencies.get_recent_activity_service(
            self.database_manager
        )
        self.unique_link_service = common_dependencies.get_miner_unique_link_service(
            self.database_manager
        )
        self.order_history_service = common_dependencies.get_order_history_service(
            self.database_manager
        )
        self.migration_service = dependencies.get_migration_service(
            self.database_manager
        )

        # Enhanced performance tracking
        self.campaign_performance = {}  # Track campaign performance
        self.sales_history = []  # Track sales history
        self.conversion_rates = {}  # Track conversion rates by campaign
        self.performance_metrics = {
            'total_sales': 0,
            'total_amount': 0,
            'avg_sale': 0,
            'conversion_rate': 0,
            'refund_rate': 0
        }
        self.last_optimization_time = datetime.utcnow()
        self.optimization_interval = timedelta(minutes=30)

        if self.config.mock:
            self.dendrite = MockDendrite(wallet=self.wallet)
        else:
            self.dendrite = bt.dendrite(wallet=self.wallet)
        bt.logging.info(f"Dendrite: {self.dendrite}")

        operations = [
            PingOperation,
            RecentActivityOperation,
            SyncVisitsOperation,
            NotifyOrderOperation,
        ]

        for operation in map(self._create_operation, operations):
            self.axon.attach(operation.forward, operation.blacklist, operation.priority)

    def sync(self):
        bt.logging.debug("Start sync")
        try:
            super().sync()
        except Exception as ex:
            bt.logging.exception(f"Error during sync: {str(ex)}")
        try:
            self.loop.run_until_complete(self._migrate_old_data())
            self.loop.run_until_complete(self._set_hotkey_and_block())
            self.loop.run_until_complete(self._ping_bitads())
            # self.loop.run_until_complete(self.__sync_visits())
            self.loop.run_until_complete(self._send_load_data())
            self.loop.run_until_complete(self._clear_recent_activity())
        except Exception as e:
            bt.logging.exception(f"Error during sync: {str(e)}")
        bt.logging.debug("End sync")

    @execute_periodically(const.PING_PERIOD)
    async def _ping_bitads(self):
        try:
            bt.logging.info("Start ping BitAds")
            response = self.bit_ads_client.subnet_ping()
            if response and response.result:
                self.validators = response.validators
                self.miners = response.miners
                
                # Get optimized campaign selection
                optimized_campaigns = await self._optimize_campaign_selection()
                await self.campaign_service.set_campaigns(optimized_campaigns)
                
                # Log performance metrics
                bt.logging.info(f"Campaign Performance: {self.campaign_performance}")
                bt.logging.info(f"Conversion Rates: {self.conversion_rates}")
                
            bt.logging.info("End ping BitAds")
        except Exception as e:
            bt.logging.exception(f"Error in _ping_bitads: {str(e)}")

    @execute_periodically(Environ.CLEAR_RECENT_ACTIVITY_PERIOD)
    async def _clear_recent_activity(self):
        try:
            bt.logging.info("Start clear recent activity")
            await self.recent_activity_service.clear_old_recent_activity()
            bt.logging.info("End clear recent activity")
        except Exception as e:
            bt.logging.exception(f"Error in _clear_recent_activity: {str(e)}")

    @execute_periodically(const.MIGRATE_OLD_DATA_PERIOD)
    async def _migrate_old_data(self):
        try:
            bt.logging.info("Start migrate old data")
            created_at_from = datetime.utcnow() - timedelta(
                seconds=ValidatorEnviron.MR_DAYS.total_seconds() * 2
            )
            await self.migration_service.migrate(created_at_from)
            bt.logging.info("End migrate old data")
        except:
            bt.logging.exception("Error while data migration")

    async def __sync_visits(self, timeout: float = 11.0):
        try:
            bt.logging.info("Start sync process")
            offset = await self.miner_service.get_last_update_visit(
                self.wallet.get_hotkey().ss58_address
            )
            bt.logging.debug(f"Sync visits with offset: {offset}")
            bt.logging.debug(f"Sync visits with miners: {self.miners}")
            responses = await forward_each_axon(
                self,
                SyncVisits(offset=offset),
                *self.miners,
                timeout=timeout,
            )
            visits = {
                visit for synapse in responses.values() for visit in synapse.visits
            }
            try:
                await self.miner_service.add_visits(visits)
            except Exception as e:
                bt.logging.exception(f"Unable to add visits: {str(e)}")
            bt.logging.info("End sync process")
        except Exception as e:
            bt.logging.exception(f"Error in __sync_visits: {str(e)}")

    @execute_periodically(timedelta(minutes=15))
    async def _send_load_data(self):
        try:
            bt.logging.info("Start send load data to BitAds")
            
            # Get current performance metrics
            performance = await self._track_sales_performance()
            
            # Add enhanced performance data to system load
            load_data = utils.get_load_average_json()
            load_data.update({
                'performance_metrics': performance,
                'campaign_performance': self.campaign_performance,
                'optimization_status': {
                    'last_optimization': self.last_optimization_time.isoformat(),
                    'next_optimization': (self.last_optimization_time + self.optimization_interval).isoformat()
                }
            })
            
            self.bit_ads_client.send_system_load(load_data)
            bt.logging.info("End send load data to BitAds")
        except Exception as e:
            bt.logging.exception(f"Error in _send_load_data: {str(e)}")

    async def _set_hotkey_and_block(self):
        try:
            current_block = self.block
            hotkey = self.wallet.get_hotkey().ss58_address
            await self.miner_service.set_hotkey_and_block(hotkey, current_block)
        except Exception as e:
            bt.logging.exception(f"Error in _set_hotkey_and_block: {str(e)}")

    def save_state(self):
        """
        Nothing to save at this moment
        """

    def _create_operation(self, op_type: Type[BaseOperation]):
        return op_type(**self.__dict__)

    async def _optimize_campaign_selection(self):
        """Enhanced campaign optimization based on performance metrics"""
        try:
            active_campaigns = await self.campaign_service.get_active_campaigns()
            current_time = datetime.utcnow()
            
            # Only perform full optimization if enough time has passed
            if current_time - self.last_optimization_time < self.optimization_interval:
                return active_campaigns
            
            self.last_optimization_time = current_time
            
            # Update performance metrics
            for campaign in active_campaigns:
                campaign_id = campaign.id
                sales = await self.order_history_service.get_campaign_sales(campaign_id)
                visits = await self.miner_service.get_campaign_visits(campaign_id)
                refunds = await self.order_history_service.get_campaign_refunds(campaign_id)
                
                # Calculate advanced metrics
                conversion_rate = len(sales) / len(visits) if visits else 0
                refund_rate = len(refunds) / len(sales) if sales else 0
                avg_sale = sum(sale.amount for sale in sales) / len(sales) if sales else 0
                
                # Update campaign performance with more detailed metrics
                self.campaign_performance[campaign_id] = {
                    'conversion_rate': conversion_rate,
                    'refund_rate': refund_rate,
                    'avg_sale': avg_sale,
                    'total_sales': len(sales),
                    'total_visits': len(visits),
                    'total_refunds': len(refunds),
                    'last_updated': current_time
                }
            
            # Enhanced campaign scoring
            def calculate_campaign_score(campaign):
                perf = self.campaign_performance.get(campaign.id, {})
                if not perf:
                    return 0
                
                # Weight factors
                sales_weight = 0.90  # Sales amount weight
                conv_weight = 0.05   # Conversion rate weight
                refund_weight = 0.05 # Refund rate weight (negative impact)
                
                # Calculate normalized scores
                sales_score = min(perf.get('avg_sale', 0) / 500, 1)  # Normalize to max $500
                conv_score = min(perf.get('conversion_rate', 0) / 0.05, 1)  # Normalize to 5%
                refund_penalty = perf.get('refund_rate', 0)  # Higher refund rate = lower score
                
                # Calculate final score
                score = (
                    sales_score * sales_weight +
                    conv_score * conv_weight -
                    refund_penalty * refund_weight
                )
                
                return max(0, score)  # Ensure score is not negative
            
            # Sort campaigns by enhanced score
            sorted_campaigns = sorted(
                active_campaigns,
                key=calculate_campaign_score,
                reverse=True
            )
            
            # Log optimization results
            bt.logging.info(f"Campaign Optimization Results:")
            for campaign in sorted_campaigns[:5]:  # Log top 5 campaigns
                score = calculate_campaign_score(campaign)
                perf = self.campaign_performance.get(campaign.id, {})
                bt.logging.info(
                    f"Campaign {campaign.id}: Score={score:.3f}, "
                    f"Avg Sale=${perf.get('avg_sale', 0):.2f}, "
                    f"Conv Rate={perf.get('conversion_rate', 0):.2%}, "
                    f"Refund Rate={perf.get('refund_rate', 0):.2%}"
                )
            
            return sorted_campaigns
        except Exception as e:
            bt.logging.exception(f"Error in campaign optimization: {str(e)}")
            return []

    async def _track_sales_performance(self):
        """Enhanced sales performance tracking"""
        try:
            # Get recent sales with extended period
            recent_sales = await self.order_history_service.get_recent_sales(
                timedelta(days=30)
            )
            
            # Get refunds
            refunds = await self.order_history_service.get_recent_refunds(
                timedelta(days=30)
            )
            
            # Update sales history
            self.sales_history = recent_sales
            
            # Calculate enhanced metrics
            total_sales = len(recent_sales)
            total_amount = sum(sale.amount for sale in recent_sales)
            avg_sale = total_amount / total_sales if total_sales > 0 else 0
            refund_rate = len(refunds) / total_sales if total_sales > 0 else 0
            
            # Update performance metrics
            self.performance_metrics = {
                'total_sales': total_sales,
                'total_amount': total_amount,
                'avg_sale': avg_sale,
                'refund_rate': refund_rate,
                'last_updated': datetime.utcnow()
            }
            
            # Log detailed performance
            bt.logging.info(
                f"Sales Performance - "
                f"Total: {total_sales}, "
                f"Amount: ${total_amount:.2f}, "
                f"Avg: ${avg_sale:.2f}, "
                f"Refund Rate: {refund_rate:.2%}"
            )
            
            return self.performance_metrics
        except Exception as e:
            bt.logging.exception(f"Error tracking sales performance: {str(e)}")
            return {}


if __name__ == "__main__":
    bt.logging.set_info()
    log_startup("Miner")
    logging.getLogger(bt.__name__).addFilter(BittensorLoggingFilter())
    with dependencies.get_core_miner() as miner:
        while True:
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                break
            except Exception as e:
                bt.logging.exception(f"Error in main loop: {str(e)}")
