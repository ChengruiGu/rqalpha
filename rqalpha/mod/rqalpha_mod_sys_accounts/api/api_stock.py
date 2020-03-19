# -*- coding: utf-8 -*-
# 版权所有 2019 深圳米筐科技有限公司（下称“米筐科技”）
#
# 除非遵守当前许可，否则不得使用本软件。
#
#     * 非商业用途（非商业用途指个人出于非商业目的使用本软件，或者高校、研究所等非营利机构出于教育、科研等目的使用本软件）：
#         遵守 Apache License 2.0（下称“Apache 2.0 许可”），
#         您可以在以下位置获得 Apache 2.0 许可的副本：http://www.apache.org/licenses/LICENSE-2.0。
#         除非法律有要求或以书面形式达成协议，否则本软件分发时需保持当前许可“原样”不变，且不得附加任何条件。
#
#     * 商业用途（商业用途指个人出于任何商业目的使用本软件，或者法人或其他组织出于任何目的使用本软件）：
#         未经米筐科技授权，任何个人不得出于任何商业目的使用本软件（包括但不限于向第三方提供、销售、出租、出借、转让本软件、
#         本软件的衍生产品、引用或借鉴了本软件功能或源代码的产品或服务），任何法人或其他组织不得出于任何目的使用本软件，
#         否则米筐科技有权追究相应的知识产权侵权责任。
#         在此前提下，对本软件的使用同样需要遵守 Apache 2.0 许可，Apache 2.0 许可与本许可冲突之处，以本许可为准。
#         详细的授权流程，请联系 public@ricequant.com 获取。

import math
from itertools import chain

from decimal import Decimal, getcontext
from typing import Dict, List, Union

import six
import pandas as pd
import numpy as np

from rqalpha.api import export_as_api
from rqalpha.apis.api_base import instruments, cal_style
from rqalpha.apis.api_abstract import order_shares, order_value, order_percent, order_target_value, order_target_percent
from rqalpha.const import (
    DEFAULT_ACCOUNT_TYPE, EXECUTION_PHASE, SIDE, ORDER_TYPE, POSITION_EFFECT, FRONT_VALIDATOR_TYPE, POSITION_DIRECTION
)
from rqalpha.environment import Environment
from rqalpha.execution_context import ExecutionContext
from rqalpha.model.instrument import Instrument
from rqalpha.model.order import Order, MarketOrder, LimitOrder
from rqalpha.interface import AbstractPosition
from rqalpha.utils import is_valid_price, INST_TYPE_IN_STOCK_ACCOUNT
from rqalpha.utils.arg_checker import apply_rules, verify_that
from rqalpha.utils.exception import RQInvalidArgument
from rqalpha.utils.i18n import gettext as _
from rqalpha.utils.logger import user_system_log

# 使用Decimal 解决浮点数运算精度问题
getcontext().prec = 10


@order_shares.register(INST_TYPE_IN_STOCK_ACCOUNT)
def stock_order_shares(id_or_ins, amount, price=None, style=None):
    if amount == 0:
        # 如果下单量为0，则认为其并没有发单，则直接返回None
        user_system_log.warn(_(u"Order Creation Failed: Order amount is 0."))
        return None
    style = cal_style(price, style)
    if isinstance(style, LimitOrder):
        if style.get_limit_price() <= 0:
            raise RQInvalidArgument(_(u"Limit order price should be positive"))
    order_book_id = assure_stock_order_book_id(id_or_ins)
    auto_switch_order_value = Environment.get_instance().config.mod.sys_accounts.auto_switch_order_value
    return _order_shares(order_book_id, amount, style, auto_switch_order_value)


def _order_shares(order_book_id, amount, style, auto_switch_order_value):
    env = Environment.get_instance()

    price = env.get_last_price(order_book_id)
    if not is_valid_price(price):
        user_system_log.warn(
            _(u"Order Creation Failed: [{order_book_id}] No market data").format(order_book_id=order_book_id))
        return

    if amount > 0:
        side = SIDE.BUY
        position_effect = POSITION_EFFECT.OPEN
    else:
        amount = abs(amount)
        side = SIDE.SELL
        position_effect = POSITION_EFFECT.CLOSE

    if side == SIDE.BUY:
        # 卖出不再限制 round_lot, order_shares 不再依赖 portfolio
        round_lot = int(env.get_instrument(order_book_id).round_lot)
        try:
            amount = int(Decimal(amount) / Decimal(round_lot)) * round_lot
        except ValueError:
            amount = 0

    r_order = Order.__from_create__(order_book_id, amount, side, style, position_effect)

    if amount == 0:
        # 如果计算出来的下单量为0, 则不生成Order, 直接返回None
        # 因为很多策略会直接在handle_bar里面执行order_target_percent之类的函数，经常会出现下一个量为0的订单，如果这些订单都生成是没有意义的。
        user_system_log.warn(_(u"Order Creation Failed: 0 order quantity"))
        return
    if r_order.type == ORDER_TYPE.MARKET:
        r_order.set_frozen_price(price)

    reject_validator_type = env.validate_order_submission(r_order)
    if not reject_validator_type:
        env.broker.submit_order(r_order)
        return r_order
    else:
        if auto_switch_order_value and reject_validator_type == FRONT_VALIDATOR_TYPE.CASH:
            remaining_cash = env.portfolio.accounts[DEFAULT_ACCOUNT_TYPE.STOCK.name].cash
            user_system_log.warn(_(
                "Insufficient cash, use all remaining cash({}) to create order").format(remaining_cash)
            )
            return _order_value(order_book_id, remaining_cash, style)


def _sell_all_stock(order_book_id, amount, style):
    env = Environment.get_instance()
    order = Order.__from_create__(order_book_id, amount, SIDE.SELL, style, POSITION_EFFECT.CLOSE)
    if amount == 0:
        user_system_log.warn(_(u"Order Creation Failed: 0 order quantity"))
        return

    if env.can_submit_order(order):
        env.broker.submit_order(order)
        return order


@export_as_api
@ExecutionContext.enforce_phase(
    EXECUTION_PHASE.OPEN_AUCTION,
    EXECUTION_PHASE.ON_BAR,
    EXECUTION_PHASE.ON_TICK,
    EXECUTION_PHASE.SCHEDULED,
    EXECUTION_PHASE.GLOBAL
)
@apply_rules(verify_that('id_or_ins').is_valid_stock(),
             verify_that('amount').is_number(),
             verify_that('style').is_instance_of((MarketOrder, LimitOrder, type(None))))
def order_lots(id_or_ins, amount, price=None, style=None):
    """
    指定手数发送买/卖单。如有需要落单类型当做一个参量传入，如果忽略掉落单类型，那么默认是市价单（market order）。

    :param id_or_ins: 下单标的物
    :type id_or_ins: :class:`~Instrument` object | `str`

    :param int amount: 下单量, 正数代表买入，负数代表卖出。将会根据一手xx股来向下调整到一手的倍数，比如中国A股就是调整成100股的倍数。

    :param float price: 下单价格，默认为None，表示 :class:`~MarketOrder`, 此参数主要用于简化 `style` 参数。

    :param style: 下单类型, 默认是市价单。目前支持的订单类型有 :class:`~LimitOrder` 和 :class:`~MarketOrder`
    :type style: `OrderStyle` object

    :return: :class:`~Order` object | None

    :example:

    .. code-block:: python

        #买入20手的平安银行股票，并且发送市价单：
        order_lots('000001.XSHE', 20)
        #买入10手平安银行股票，并且发送限价单，价格为￥10：
        order_lots('000001.XSHE', 10, style=LimitOrder(10))

    """
    order_book_id = assure_stock_order_book_id(id_or_ins)

    round_lot = int(Environment.get_instance().get_instrument(order_book_id).round_lot)

    style = cal_style(price, style)

    return order_shares(id_or_ins, amount * round_lot, style=style)


@order_value.register(INST_TYPE_IN_STOCK_ACCOUNT)
def stock_order_value(id_or_ins, cash_amount, price=None, style=None):
    style = cal_style(price, style)

    if isinstance(style, LimitOrder):
        if style.get_limit_price() <= 0:
            raise RQInvalidArgument(_(u"Limit order price should be positive"))

    order_book_id = assure_stock_order_book_id(id_or_ins)
    return _order_value(order_book_id, cash_amount, style)


def _order_value(order_book_id, cash_amount, style):
    env = Environment.get_instance()

    price = env.get_last_price(order_book_id)
    if not is_valid_price(price):
        user_system_log.warn(
            _(u"Order Creation Failed: [{order_book_id}] No market data").format(order_book_id=order_book_id))
        return

    account = env.portfolio.accounts[DEFAULT_ACCOUNT_TYPE.STOCK.name]

    if cash_amount > 0:
        cash_amount = min(cash_amount, account.cash)

    price = price if isinstance(style, MarketOrder) else style.get_limit_price()
    amount = int(Decimal(cash_amount) / Decimal(price))

    if cash_amount > 0:
        round_lot = int(env.get_instrument(order_book_id).round_lot)

        # FIXME: logic duplicate with order_shares
        amount = int(Decimal(amount) / Decimal(round_lot)) * round_lot

        while amount > 0:
            dummy_order = Order.__from_create__(order_book_id, amount, SIDE.BUY, LimitOrder(price),
                                                POSITION_EFFECT.OPEN)
            expected_transaction_cost = env.get_order_transaction_cost(dummy_order)
            if amount * price + expected_transaction_cost <= cash_amount:
                break
            amount -= round_lot
        else:
            user_system_log.warn(_(u"Order Creation Failed: 0 order quantity"))
            return

    # if the cash_amount is larger than you current security’s position,
    # then it will sell all shares of this security.

    position = account.get_position(order_book_id, POSITION_DIRECTION.LONG)
    amount = downsize_amount(amount, position)

    return _order_shares(order_book_id, amount, style, auto_switch_order_value=False)


@order_percent.register(INST_TYPE_IN_STOCK_ACCOUNT)
def stock_order_percent(id_or_ins, percent, price=None, style=None):
    if percent < -1 or percent > 1:
        raise RQInvalidArgument(_(u"percent should between -1 and 1"))

    style = cal_style(price, style)
    account = Environment.get_instance().portfolio.accounts[DEFAULT_ACCOUNT_TYPE.STOCK.name]
    return order_value(id_or_ins, account.total_value * percent, style=style)


@order_target_value.register(INST_TYPE_IN_STOCK_ACCOUNT)
def stock_order_target_value(id_or_ins, cash_amount, price=None, style=None):
    order_book_id = assure_stock_order_book_id(id_or_ins)
    account = Environment.get_instance().portfolio.accounts[DEFAULT_ACCOUNT_TYPE.STOCK.name]
    position = account.get_position(order_book_id, POSITION_DIRECTION.LONG)  # type: AbstractPosition

    style = cal_style(price, style)
    if cash_amount == 0:
        return _sell_all_stock(order_book_id, position.closable, style)

    try:
        market_value = position.market_value
    except RuntimeError:
        order_result = order_value(order_book_id, np.nan, style=style)
        if order_result:
            raise
    else:
        return order_value(order_book_id, cash_amount - market_value, style=style)


@order_target_percent.register(INST_TYPE_IN_STOCK_ACCOUNT)
def stock_order_target_percent(id_or_ins, percent, price=None, style=None):
    order_book_id = assure_stock_order_book_id(id_or_ins)

    style = cal_style(price, style)

    account = Environment.get_instance().portfolio.accounts[DEFAULT_ACCOUNT_TYPE.STOCK]
    position = account.get_position(order_book_id, POSITION_DIRECTION.LONG)  # type: AbstractPosition

    if percent == 0:
        return _sell_all_stock(order_book_id, position.closable, style)

    try:
        market_value = position.market_value
    except RuntimeError:
        order_result = order_value(order_book_id, np.nan, style=style)
        if order_result:
            raise
    else:
        return order_value(order_book_id, account.total_value * percent - market_value, style=style)


@export_as_api
@ExecutionContext.enforce_phase(
    EXECUTION_PHASE.OPEN_AUCTION,
    EXECUTION_PHASE.ON_BAR,
    EXECUTION_PHASE.ON_TICK,
    EXECUTION_PHASE.SCHEDULED,
    EXECUTION_PHASE.GLOBAL
)
def order_target_portfolio(target_portfolio):
    # type: (Dict[Union[str, Instrument]: float]) -> List[Order]

    if isinstance(target_portfolio, pd.Series):
        # FIXME: kind of dirty
        total_percent = sum(target_portfolio)
    else:
        total_percent = sum(six.itervalues(target_portfolio))
    if total_percent > 1:
        raise RQInvalidArgument(_(u"total percent should be lower than 1, current: {}").format(total_percent))

    env = Environment.get_instance()
    account = env.portfolio.accounts[DEFAULT_ACCOUNT_TYPE.STOCK]
    account_value = account.total_value
    target_quantities = {}
    for id_or_ins, target_percent in six.iteritems(target_portfolio):
        order_book_id = assure_stock_order_book_id(id_or_ins)
        if target_percent < 0:
            raise RQInvalidArgument(_(u"target percent of should {} between 0 and 1, current: {}").format(
                order_book_id, target_percent
            ))
        price = env.data_proxy.get_last_price(order_book_id)
        if not is_valid_price(price):
            user_system_log.warn(
                _(u"Order Creation Failed: [{order_book_id}] No market data").format(order_book_id=order_book_id)
            )
            continue
        target_quantities[order_book_id] = account_value * target_percent / price

    close_orders, open_orders = [], []
    current_quantities = {
        p.order_book_id: p.quantity for p in account.get_positions() if p.direction == POSITION_DIRECTION.LONG
    }
    for order_book_id, quantity in six.iteritems(current_quantities):
        if order_book_id not in target_portfolio:
            close_orders.append(Order.__from_create__(
                order_book_id, quantity, SIDE.SELL, MarketOrder(), POSITION_EFFECT.CLOSE
            ))

    round_lot = 100
    for order_book_id, target_quantity in six.iteritems(target_quantities):
        if order_book_id in current_quantities:
            delta_quantity = target_quantity - current_quantities[order_book_id]
        else:
            delta_quantity = target_quantity

        if delta_quantity >= round_lot:
            delta_quantity = math.floor(delta_quantity / round_lot) * round_lot
            open_orders.append(Order.__from_create__(
                order_book_id, delta_quantity, SIDE.BUY, MarketOrder(), POSITION_EFFECT.OPEN
            ))
        elif delta_quantity < -1:
            delta_quantity = math.floor(delta_quantity)
            close_orders.append(Order.__from_create__(
                order_book_id, abs(delta_quantity), SIDE.SELL, MarketOrder(), POSITION_EFFECT.CLOSE
            ))

    # TODO: 下一个 bar 撮合资金咋回流？实盘咋办？要求用户多次调用？
    submit_orders = []
    for order in chain(close_orders, open_orders):
        if env.can_submit_order(order):
            submit_orders.append(order)
            env.broker.submit_order(order)
    return submit_orders


@export_as_api
@ExecutionContext.enforce_phase(EXECUTION_PHASE.ON_INIT,
                                EXECUTION_PHASE.BEFORE_TRADING,
                                EXECUTION_PHASE.OPEN_AUCTION,
                                EXECUTION_PHASE.ON_BAR,
                                EXECUTION_PHASE.ON_TICK,
                                EXECUTION_PHASE.AFTER_TRADING,
                                EXECUTION_PHASE.SCHEDULED)
@apply_rules(verify_that('order_book_id').is_valid_instrument(),
             verify_that('count').is_greater_than(0))
def is_suspended(order_book_id, count=1):
    """
    判断某只股票是否全天停牌。

    :param str order_book_id: 某只股票的代码或股票代码，可传入单只股票的order_book_id, symbol

    :param int count: 回溯获取的数据个数。默认为当前能够获取到的最近的数据

    :return: count为1时 `bool`; count>1时 `pandas.DataFrame`
    """
    dt = Environment.get_instance().calendar_dt.date()
    order_book_id = assure_stock_order_book_id(order_book_id)
    return Environment.get_instance().data_proxy.is_suspended(order_book_id, dt, count)


@export_as_api
@ExecutionContext.enforce_phase(EXECUTION_PHASE.ON_INIT,
                                EXECUTION_PHASE.BEFORE_TRADING,
                                EXECUTION_PHASE.OPEN_AUCTION,
                                EXECUTION_PHASE.ON_BAR,
                                EXECUTION_PHASE.ON_TICK,
                                EXECUTION_PHASE.AFTER_TRADING,
                                EXECUTION_PHASE.SCHEDULED)
@apply_rules(verify_that('order_book_id').is_valid_instrument())
def is_st_stock(order_book_id, count=1):
    """
    判断股票在一段时间内是否为ST股（包括ST与*ST）。

    ST股是有退市风险因此风险比较大的股票，很多时候您也会希望判断自己使用的股票是否是'ST'股来避开这些风险大的股票。另外，我们目前的策略比赛也禁止了使用'ST'股。

    :param str order_book_id: 某只股票的代码，可传入单只股票的order_book_id, symbol

    :param int count: 回溯获取的数据个数。默认为当前能够获取到的最近的数据

    :return: count为1时 `bool`; count>1时 `pandas.DataFrame`
    """
    dt = Environment.get_instance().calendar_dt.date()
    order_book_id = assure_stock_order_book_id(order_book_id)
    return Environment.get_instance().data_proxy.is_st_stock(order_book_id, dt, count)


def assure_stock_order_book_id(id_or_symbols):
    if isinstance(id_or_symbols, Instrument):
        return id_or_symbols.order_book_id
    elif isinstance(id_or_symbols, six.string_types):
        return assure_stock_order_book_id(instruments(id_or_symbols))
    else:
        raise RQInvalidArgument(_(u"unsupported order_book_id type"))


def downsize_amount(amount, position):
    # type: (float, AbstractPosition) -> float
    config = Environment.get_instance().config
    if not config.validator.close_amount:
        return amount
    if amount > 0:
        return amount
    else:
        amount = abs(amount)
        if amount > position.closable:
            return -position.closable
        else:
            return -amount
