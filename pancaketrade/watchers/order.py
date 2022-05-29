"""Order watcher."""
from decimal import Decimal
from typing import Optional

from loguru import logger
from pancaketrade.network import Network
from pancaketrade.persistence import Order, Token, db
from pancaketrade.utils.generic import format_token_amount, start_in_thread
from telegram.ext import Dispatcher
from web3.types import Wei


class OrderWatcher:
    def __init__(self, order_record: Order, net: Network, dispatcher: Dispatcher, chat_id: int):
        self.order_record = order_record
        self.token_record: Token = order_record.token
        self.net = net
        self.dispatcher = dispatcher
        self.chat_id = chat_id

        self.type = order_record.type  # buy (tokens for BNB) or sell (tokens for BNB)
        self.limit_price: Optional[Decimal] = (
            Decimal(order_record.limit_price) if order_record.limit_price else None
        )  # decimal stored as string
        self.above = order_record.above  # Above = True, below = False
        self.trailing_stop: Optional[int] = order_record.trailing_stop  # in percent
        self.amount = Wei(int(order_record.amount))  # in wei, either BNB (buy) or token (sell) depending on "type"
        self.slippage = Decimal(order_record.slippage)  # in percent
        # gas price in wei or offset from default in gwei (starts with +), if null then use network gas price
        self.gas_price: Optional[str] = order_record.gas_price
        self.created = order_record.created
        self.active = True
        self.finished = False
        self.min_price: Optional[Decimal] = None
        self.max_price: Optional[Decimal] = None

    def __str__(self) -> str:
        type_name = self.get_type_name()
        comparison = self.get_comparison_symbol()
        amount = self.get_human_amount()
        unit = self.get_amount_unit()
        trailing = f' tsl {self.trailing_stop}%' if self.trailing_stop is not None else ''
        order_id = f'<u>#{self.order_record.id}</u>' if self.min_price or self.max_price else f'#{self.order_record.id}'
        limit_price = f'<code>{self.limit_price:.3g}</code> BNB' if self.limit_price is not None else 'market price'
        type_icon = self.get_type_icon()
        return (
            f'{type_icon} {order_id}: {self.token_record.symbol} {comparison} {limit_price} - '
            + f'<b>{type_name}</b> <code>{format_token_amount(amount)}</code> {unit}{trailing}'
        )

    def long_str(self) -> str:
        icon = self.token_record.icon + ' ' if self.token_record.icon else ''
        type_name = self.get_type_name()
        comparision = self.get_comparison_symbol()
        amount = self.get_human_amount()
        unit = self.get_amount_unit()
        trailing = f'Trailing stop loss {self.trailing_stop}% callback\n' if self.trailing_stop is not None else ''
        gas_price = (
            f'{Decimal(self.gas_price) / Decimal(10 ** 9):.1f} Gwei'
            if self.gas_price and not self.gas_price.startswith('+')
            else 'network default'
            if self.gas_price is None
            else f'network default {self.gas_price} Gwei'
        )
        order_id = f'<u>#{self.order_record.id}</u>' if self.min_price or self.max_price else f'#{self.order_record.id}'
        type_icon = self.get_type_icon()
        limit_price = f'<code>{self.limit_price:.3g}</code> BNB' if self.limit_price is not None else 'market price'
        return (
            f'{icon}{self.token_record.symbol} - ({order_id}) <b>{type_name}</b> {type_icon}\n'
            + f'<b>Amount</b>: <code>{format_token_amount(amount)}</code> {unit}\n'
            + f'<b>Price</b>: {comparision} {limit_price}\n'
            + trailing
            + f'<b>Slippage</b>: {self.slippage}%\n'
            + f'<b>Gas</b>: {gas_price}\n'
            + f'<b>Created</b>: {self.created.strftime("%Y-%m-%d %H:%m")}'
        )

    def price_update(self, sell_price: Decimal, buy_price: Decimal, sell_v2: bool, buy_v2: bool):
        if not self.active:
            return

        if self.type == 'buy':
            self.price_update_buy(buy_price=buy_price, sell_v2=sell_v2, buy_v2=buy_v2)
        else:
            self.price_update_sell(sell_price=sell_price, sell_v2=sell_v2, buy_v2=buy_v2)

    def price_update_buy(self, buy_price: Decimal, sell_v2: bool, buy_v2: bool):
        if buy_price == 0:
            logger.warning(f'Price of {self.token_record.symbol} is zero or not available')
            return
        limit_price = (
            self.limit_price if self.limit_price is not None else buy_price
        )  # fulfill condition immediately if we have no limit price
        if self.trailing_stop is None and not self.above and buy_price <= limit_price:
            logger.success(f'Limit buy triggered at price {buy_price:.3e} BNB')  # buy
            self.close(sell_v2=sell_v2, buy_v2=buy_v2)
            return
        elif self.trailing_stop and not self.above and (buy_price <= limit_price or self.min_price is not None):
            if self.min_price is None:
                logger.info(f'Limit condition reached at price {buy_price:.3e} BNB')
                self.dispatcher.bot.send_message(
                    chat_id=self.chat_id, text=f'🔹 Order #{self.order_record.id} activated trailing stop loss.'
                )
                self.min_price = buy_price
            rise = ((buy_price / self.min_price) - Decimal(1)) * Decimal(100)
            if buy_price < self.min_price:
                self.min_price = buy_price
                return
            elif rise > self.trailing_stop:
                logger.success(f'Trailing stop loss triggered at price {buy_price:.3e} BNB')  # buy
                self.close(sell_v2=sell_v2, buy_v2=buy_v2)
                return

    def price_update_sell(self, sell_price: Decimal, sell_v2: bool, buy_v2: bool):
        if sell_price == 0:
            logger.warning(f'Price of {self.token_record.symbol} is zero or not available')
            return
        limit_price = (
            self.limit_price if self.limit_price is not None else sell_price
        )  # fulfill condition immediately if we have no limit price
        if self.trailing_stop is None and not self.above and sell_price <= limit_price:
            logger.warning(f'Stop loss triggered at price {sell_price:.3e} BNB')
            self.close(sell_v2=sell_v2, buy_v2=buy_v2)
            return
        elif self.trailing_stop is None and self.above and sell_price >= limit_price:
            logger.success(f'Take profit triggered at price {sell_price:.3e} BNB')
            self.close(sell_v2=sell_v2, buy_v2=buy_v2)
            return
        elif self.trailing_stop and self.above and (sell_price >= limit_price or self.max_price is not None):
            if self.max_price is None:
                logger.info(f'Limit condition reached at price {sell_price:.3e} BNB')
                self.dispatcher.bot.send_message(
                    chat_id=self.chat_id, text=f'🔹 Order #{self.order_record.id} activated trailing stop loss.'
                )
                self.max_price = sell_price
            drop = (Decimal(1) - (sell_price / self.max_price)) * Decimal(100)
            if sell_price > self.max_price:
                self.max_price = sell_price
                return
            elif drop > self.trailing_stop:
                logger.success(f'Trailing stop loss triggered at price {sell_price:.3e} BNB')
                self.close(sell_v2=sell_v2, buy_v2=buy_v2)
                return

    def close(self, sell_v2: bool, buy_v2: bool):
        self.active = False

        if self.type == 'buy':
            version = 'v2' if buy_v2 else 'v1'
            logger.info(f'Buying tokens on {version}')
            amount = Decimal(self.amount) / Decimal(10 ** 18)
            self.dispatcher.bot.send_message(
                chat_id=self.chat_id,
                text=f'🔸 Trying to buy for {format_token_amount(amount)} BNB of {self.token_record.symbol}...',
            )
            start_in_thread(self.buy, args=(buy_v2, sell_v2))
        else:  # sell
            version = 'v2' if sell_v2 else 'v1'
            logger.info(f'Selling tokens on {version}')
            amount = Decimal(self.amount) / Decimal(10 ** self.token_record.decimals)
            self.dispatcher.bot.send_message(
                chat_id=self.chat_id,
                text=f'🔸 Trying to sell {format_token_amount(amount)} {self.token_record.symbol}...',
            )
            start_in_thread(self.sell, args=(sell_v2,))

    def buy(self, v2: bool, sell_v2: bool):
        balance_before = self.net.get_token_balance(token_address=self.token_record.address)
        buy_price_before = self.token_record.effective_buy_price
        res, tokens_out, txhash_or_error = self.net.buy_tokens(
            self.token_record.address,
            amount_bnb=self.amount,
            slippage_percent=self.slippage,
            gas_price=self.gas_price,
            v2=v2,
        )
        if not res:
            if len(txhash_or_error) == 66:
                reason_or_link = f'<a href="https://bscscan.com/tx/{txhash_or_error}">{txhash_or_error[:8]}...</a>'
            else:
                reason_or_link = txhash_or_error
            logger.error(f'Transaction failed: {reason_or_link}')
            self.dispatcher.bot.send_message(
                chat_id=self.chat_id,
                text=f'⛔️ <u>Transaction failed:</u> {txhash_or_error}\n' + 'Order below deleted:\n' + self.long_str(),
            )
            # self.remove_order()
            # self.finished = True  # will trigger deletion of the object
            return
        effective_price = self.get_human_amount() / tokens_out
        db.connect()
        try:
            with db.atomic():
                if buy_price_before is not None:
                    self.token_record.effective_buy_price = str(
                        (balance_before * Decimal(buy_price_before) + tokens_out * effective_price)
                        / (balance_before + tokens_out)
                    )
                else:
                    self.token_record.effective_buy_price = str(effective_price)
                self.token_record.save()
        except Exception as e:
            logger.error(f'Effective buy price update failed: {e}')
            self.dispatcher.bot.send_message(
                chat_id=self.chat_id,
                text=f'⛔️ Effective buy price update failed: {e}',
            )
        finally:
            db.close()
        logger.success(
            f'Buy transaction succeeded. Received {format_token_amount(tokens_out)} {self.token_record.symbol}. '
            + f'Effective price (after tax) {effective_price:.4g} BNB/token'
        )
        self.dispatcher.bot.send_message(
            chat_id=self.chat_id, text='<u>Closing the following order:</u>\n' + self.long_str()
        )
        self.dispatcher.bot.send_message(
            chat_id=self.chat_id,
            text=f'✅ Got {format_token_amount(tokens_out)} {self.token_record.symbol} at '
            + f'tx <a href="https://bscscan.com/tx/{txhash_or_error}">{txhash_or_error[:8]}...</a>\n'
            + f'Effective price (after tax) {effective_price:.4g} BNB/token',
        )
        if not self.net.is_approved(token_address=self.token_record.address, v2=sell_v2):
            # pre-approve for later sell
            version = 'v2' if sell_v2 else 'v1'
            logger.info(f'Approving {self.token_record.symbol} for trading on PancakeSwap {version}.')
            self.dispatcher.bot.send_message(
                chat_id=self.chat_id,
                text=f'Approving {self.token_record.symbol} for trading on PancakeSwap {version}...',
            )
            res = self.net.approve(token_address=self.token_record.address, v2=sell_v2)
            if res:
                self.dispatcher.bot.send_message(
                    chat_id=self.chat_id,
                    text='✅ Approval successful!',
                )
            else:
                self.dispatcher.bot.send_message(
                    chat_id=self.chat_id,
                    text='⛔ Approval failed',
                )
        self.remove_order()
        self.finished = True  # will trigger deletion of the object

    def sell(self, v2: bool):
        balance_before = self.net.get_token_balance_wei(token_address=self.token_record.address)
        res, bnb_out, txhash_or_error = self.net.sell_tokens(
            self.token_record.address,
            amount_tokens=self.amount,
            slippage_percent=self.slippage,
            gas_price=self.gas_price,
            v2=v2,
        )
        if not res:
            logger.error(f'Transaction failed: {txhash_or_error}')
            if len(txhash_or_error) == 66:
                reason_or_link = f'<a href="https://bscscan.com/tx/{txhash_or_error}">{txhash_or_error[:8]}...</a>'
            else:
                reason_or_link = txhash_or_error
            self.dispatcher.bot.send_message(
                chat_id=self.chat_id,
                text=f'⛔️ <u>Transaction failed:</u> {reason_or_link}\n' + 'Order below deleted.\n' + self.long_str(),
            )
            # self.remove_order()
            # self.finished = True  # will trigger deletion of the object
            return
        effective_price = bnb_out / self.get_human_amount()
        sold_proportion = self.amount / balance_before
        logger.success(
            f'Sell transaction succeeded. Received {bnb_out:.3g} BNB. '
            + f'Effective price (after tax) {effective_price:.4g} BNB/token'
        )
        self.dispatcher.bot.send_message(
            chat_id=self.chat_id, text='<u>Closing the following order:</u>\n' + self.long_str()
        )
        usd_out = self.net.get_bnb_price() * bnb_out
        self.dispatcher.bot.send_message(
            chat_id=self.chat_id,
            text=f'✅ Got {bnb_out:.3g} BNB (${usd_out:.2f}) at '
            + f'tx <a href="https://bscscan.com/tx/{txhash_or_error}">{txhash_or_error[:8]}...</a>\n'
            + f'Effective price (after tax) {effective_price:.4g} BNB/token.\n'
            + f'This order sold {sold_proportion:.1%} of the token\'s balance.',
        )
        self.remove_order()
        self.finished = True  # will trigger deletion of the object

    def get_type_name(self) -> str:
        return (
            'limit buy'
            if self.type == 'buy' and not self.above
            else 'stop loss'
            if self.type == 'sell' and not self.above
            else 'limit sell'
            if self.type == 'sell' and self.above
            else 'unknown'
        )

    def get_type_icon(self) -> str:
        return (
            '💵'
            if self.type == 'buy' and not self.above
            else '🚫'
            if self.type == 'sell' and not self.above
            else '💰'
            if self.type == 'sell' and self.above
            else ''
        )

    def get_comparison_symbol(self) -> str:
        return '=' if self.limit_price is None else '&gt;' if self.above else '&lt;'

    def get_human_amount(self) -> Decimal:
        decimals = self.token_record.decimals if self.type == 'sell' else 18
        return Decimal(self.amount) / Decimal(10 ** decimals)

    def get_amount_unit(self) -> str:
        return self.token_record.symbol if self.type == 'sell' else 'BNB'

    def remove_order(self):
        db.connect()
        try:
            self.order_record.delete_instance()
        except Exception as e:
            logger.error(f'Database error: {e}')
        finally:
            db.close()
