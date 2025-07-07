import base64
import hmac
import json
import time
from collections import deque
from urllib.parse import urlencode

import requests


class BitgetClient:
    """Base Bitget client for HTTP operations and authentication.
    
    API Doc: https://www.bitget.com/api-doc/common/signature
    """
    
    API_HOST = "https://api.bitget.com"
    RATE_LIMIT_PER_SECOND = 10 
    
    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self._request_times = deque()  
    
    def _rate_limit(self) -> None:
        """Enforce rate limiting - max 10 requests per second."""
        current_time = time.time()
        
        while self._request_times and current_time - self._request_times[0] > 1.0:
            self._request_times.popleft()
        
        if len(self._request_times) >= self.RATE_LIMIT_PER_SECOND:
            sleep_time = 1.0 - (current_time - self._request_times[0])
            if sleep_time > 0:
                time.sleep(sleep_time)
                current_time = time.time()
                while self._request_times and current_time - self._request_times[0] > 1.0:
                    self._request_times.popleft()
        
        self._request_times.append(current_time)
    
    def _timestamp_ms(self) -> str:
        return str(int(time.time() * 1000))
    
    def _sign(self, ts: str, method: str, path: str, query: str = "", body: str = "") -> str:
        prehash = f"{ts}{method.upper()}{path}{query}{body}"
        digest = hmac.new(self.api_secret.encode(), prehash.encode(), digestmod="sha256").digest()
        return base64.b64encode(digest).decode()
    
    def _headers(self, ts: str, signature: str) -> dict:
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
    
    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None):
        self._rate_limit()
        
        params = params or {}
        body = body or {}
        query = "?" + urlencode(sorted(params.items())) if params else ""
        body_s = json.dumps(body) if body else ""

        ts = self._timestamp_ms()
        sign = self._sign(ts, method, path, query, body_s)
        url = f"{self.API_HOST}{path}{query}"

        r = requests.request(method, url, headers=self._headers(ts, sign), data=body_s, timeout=10)
        
        if r.status_code != 200:
            print(f"❌ HTTP {r.status_code} Error Response: {r.text}")
        
        r.raise_for_status()

        payload = r.json()
        if payload.get("code") != "00000":
            print(f"❌ API Error Response: {payload}")
            raise RuntimeError(f"Erreur API Bitget: {payload}")

        return payload["data"]


class BitgetEarn(BitgetClient):
    """Bitget Earn operations client.
    
    API Doc: https://www.bitget.com/api-doc/earn/intro
    """
    
    def _get_flexible_product_id(self, coin: str) -> str:
        """Get active flexible savings product ID for coin.
        
        API Doc: https://www.bitget.com/api-doc/earn/savings/Savings-Products
        """
        products = self._request(
            "GET",
            "/api/v2/earn/savings/product",
            params={"coin": coin.upper(), "filter": "available_and_held"}
        )
        for p in products:
            if p["periodType"] == "flexible" and p["status"] == "in_progress":
                return p["productId"]

        raise ValueError(f"No active flexible savings product found for {coin.upper()}")

    def _get_savings_assets(
        self,
        period_type: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: str | None = None,
        id_less_than: str | None = None
    ) -> dict:
        """Get savings assets by criteria.
        
        API Doc: https://www.bitget.com/api-doc/earn/savings/Savings-Assets
        """
        params = {
            "periodType": period_type,
            **{k: v for k, v in {
                "startTime": start_time,
                "endTime": end_time,
                "limit": limit,
                "idLessThan": id_less_than,
            }.items() if v is not None}
        }

        return self._request("GET", "/api/v2/earn/savings/assets", params=params)
    
    def get_flexible_savings_amount(self, coin: str = "USDT") -> str:
        """Get flexible savings amount for coin."""
        data = self._get_savings_assets("flexible")
        
        for asset in data.get("resultList", []):
            if asset.get("productCoin", "").upper() == coin.upper():
                return asset.get("holdAmount", "0")
        
        return "0"
    
    def subscribe_savings(self, coin: str, amount: float) -> str:
        """Subscribe to flexible savings. Returns order ID.
        
        API Doc: https://www.bitget.com/api-doc/earn/savings/Savings-Subscribe
        """
        try:
            pid = self._get_flexible_product_id(coin)
            result = self._request(
                "POST",
                "/api/v2/earn/savings/subscribe",
                body={"periodType": "flexible", "productId": pid, "amount": str(amount)}
            )
            return result["orderId"]
        except Exception as e:
            raise Exception(f"❌ Failed to subscribe to savings for {coin}: {str(e)}") from e

    def redeem_savings(self, coin: str, amount: float) -> str:
        """Redeem from flexible savings. Returns order ID.
        
        API Doc: https://www.bitget.com/api-doc/earn/savings/Savings-Redeem
        """
        try:
            pid = self._get_flexible_product_id(coin)
            result = self._request(
                "POST",
                "/api/v2/earn/savings/redeem",
                body={"periodType": "flexible", "productId": pid, "amount": str(amount)}
            )
            return result["orderId"]
        except Exception as e:
            raise Exception(f"❌ Failed to redeem from savings for {coin}: {str(e)}") from e


class BitgetEarnSpot(BitgetEarn):
    """Bitget Spot trading with Earn integration.
    
    API Doc: https://www.bitget.com/api-doc/spot/intro
    """

    # Sleep times in seconds
    WAIT_AFTER_REDEEM_BEFORE_BUY = 2
    WAIT_AFTER_SELL_BEFORE_SAVINGS_DEPOSIT = 2

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        super().__init__(api_key, api_secret, passphrase)
        symbols = self._get_symbol_info()
        self._symbol_info = {s["symbol"]: s for s in symbols}
    

    def _get_symbol_info(self) -> list[dict]:
        """Get spot trading symbols info.
        
        API Doc: https://www.bitget.com/api-doc/spot/market/Get-Symbols
        """
        return self._request("GET", "/api/v2/spot/public/symbols")
    
    def _price_to_precision(self, symbol: str, price: float) -> str:
        """Adjust price to symbol precision."""
        price = float(price)
        price_precision = int(self._symbol_info[symbol]["pricePrecision"])
        return f"{price:.{price_precision}f}"

    def _amount_to_precision(self, symbol: str, amount: float) -> str:
        """Adjust amount to symbol precision."""
        amount = float(amount)
        quantity_precision = int(self._symbol_info[symbol]["quantityPrecision"])
        multiplier = 10 ** quantity_precision
        truncated_amount = int(amount * multiplier) / multiplier
        return f"{truncated_amount:.{quantity_precision}f}"
    
    def _quote_amount_to_precision(self, symbol: str, amount: float) -> str:
        """Adjust quote amount to symbol's quote precision."""
        amount = float(amount)
        quote_precision = int(self._symbol_info[symbol]["quotePrecision"])
        multiplier = 10 ** quote_precision
        truncated_amount = int(amount * multiplier) / multiplier
        return f"{truncated_amount:.{quote_precision}f}"

    
    def _get_order_info(self, order_id: str, client_oid: str | None = None) -> dict:
        """Get spot order info by ID.
        
        API Doc: https://www.bitget.com/api-doc/spot/trade/Get-Order-Info
        """
        params = {k: v for k, v in {
            "orderId": order_id,
            "clientOid": client_oid,
        }.items() if v is not None}
            
        return self._request("GET", "/api/v2/spot/trade/orderInfo", params=params)

    def get_balance(self, coin: str = "USDT", *, asset_type: str = "hold_only") -> dict:
        """Get spot account balance for coin.
        
        API Doc: https://www.bitget.com/api-doc/spot/account/Get-Account-Assets
        """
        data = self._request(
            "GET",
            "/api/v2/spot/account/assets",
            params={"coin": coin.upper(), "assetType": asset_type}
        )
        return data[0] if data else {}    
    
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        force: str,
        size: float,
        *,
        price: float | None = None,
        client_oid: str | None = None,
        trigger_price: float | None = None,
        tpsl_type: str | None = None,
        request_time: str | None = None,
        receive_window: str | None = None,
        stp_mode: str | None = None,
        preset_take_profit_price: float | None = None,
        execute_take_profit_price: float | None = None,
        preset_stop_loss_price: float | None = None,
        execute_stop_loss_price: float | None = None
    ) -> dict:
        """Place spot order with automatic precision adjustment.
        
        API Doc: https://www.bitget.com/api-doc/spot/trade/Place-Order
        """
        if order_type == "limit" and price is None:
            raise ValueError("Price required for limit orders")
        
        if order_type == "market" and side == "buy":
            size_adjusted = self._quote_amount_to_precision(symbol, size)
        else:
            size_adjusted = self._amount_to_precision(symbol, size)
            
        price_adjusted = None
        if price is not None:
            price_adjusted = self._price_to_precision(symbol, price)
        
        body = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "size": size_adjusted,
            **{k: v for k, v in {
                "price": price_adjusted,
                "force": force if order_type == "limit" else None,
                "clientOid": client_oid,
                "triggerPrice": str(trigger_price) if trigger_price is not None else None,
                "tpslType": tpsl_type,
                "requestTime": request_time,
                "receiveWindow": receive_window,
                "stpMode": stp_mode,
                "presetTakeProfitPrice": str(preset_take_profit_price) if preset_take_profit_price is not None else None,
                "executeTakeProfitPrice": str(execute_take_profit_price) if execute_take_profit_price is not None else None,
                "presetStopLossPrice": str(preset_stop_loss_price) if preset_stop_loss_price is not None else None,
                "executeStopLossPrice": str(execute_stop_loss_price) if execute_stop_loss_price is not None else None,
            }.items() if v is not None}
        }
        
        return self._request("POST", "/api/v2/spot/trade/place-order", body=body)

    def buy_from_savings(
        self,
        symbol: str,
        quote_coin: str,
        amount: float,
        *,
        order_type: str = "market",
        price: float | None = None,
        client_oid: str | None = None,
        force: str = "gtc"
    ) -> dict:
        """Redeem from savings and place buy order.
        
        Combines: Redeem-Savings + Place-Order
        """
        print(f"Redeeming {amount} {quote_coin} from savings...")
        
        redeem_order_id = self.redeem_savings(quote_coin, amount)
        print(f"Redemption complete. Order ID: {redeem_order_id}")
        
        print(f"Waiting {self.WAIT_AFTER_REDEEM_BEFORE_BUY} seconds after redeem...")
        time.sleep(self.WAIT_AFTER_REDEEM_BEFORE_BUY)
        
        print(f"Placing {order_type} buy order for {symbol}...")
        
        if order_type == "limit" and price is not None:
            base_amount = float(amount) / float(price)
            order_size = base_amount
        else:
            order_size = amount
        
        buy_result = self.place_order(
            symbol=symbol,
            side="buy",
            order_type=order_type,
            force=force,
            size=order_size,
            price=price,
            client_oid=client_oid
        )
        
        print("Buy order placed.")
        
        return {
            "redeem_order_id": redeem_order_id,
            "buy_order_id": buy_result["orderId"],
            "buy_client_oid": buy_result.get("clientOid")
        }

    def sell_to_savings(
        self,
        symbol: str,
        amount_base: float,
        quote_coin: str = "USDT",
        *,
        order_type: str = "market",
        price: float | None = None,
        client_oid: str | None = None,
        force: str = "gtc",
    ) -> dict:
        """Place sell order and deposit proceeds to savings.
        
        Combines: Place-Order + Subscribe-Savings
        """
        print(f"Placing {order_type} sell order for {amount_base} {symbol}...")
        
        sell_result = self.place_order(
            symbol=symbol,
            side="sell",
            order_type=order_type,
            force=force,
            size=amount_base,
            price=price,
            client_oid=client_oid
        )
        
        print("Sell order placed.")
        
        print(f"Waiting {self.WAIT_AFTER_SELL_BEFORE_SAVINGS_DEPOSIT} seconds before depositing to savings...")
        time.sleep(self.WAIT_AFTER_SELL_BEFORE_SAVINGS_DEPOSIT)
        
        order_info_list = self._get_order_info(sell_result['orderId'])
        
        if order_info_list and len(order_info_list) > 0:
            order_info = order_info_list[0]
            
            if order_info.get('status') == 'filled':
                quote_received = order_info.get('quoteVolume', '0')
                
                if float(quote_received) > 0:
                    formatted_amount = self._quote_amount_to_precision(symbol, quote_received)
                    print(f"Depositing {formatted_amount} {quote_coin} to savings...")
                    deposit_order_id = self.subscribe_savings(quote_coin, formatted_amount)
                    print(f"Savings deposit complete. Order ID: {deposit_order_id}")
                else:
                    print(f"No {quote_coin} received from sale")
                    deposit_order_id = None
            else:
                print(f"Order not filled (status: {order_info.get('status', 'unknown')})")
                deposit_order_id = None
        else:
            print("Unable to retrieve order information")
            deposit_order_id = None
        
        return {
            "sell_order_id": sell_result["orderId"],
            "sell_client_oid": sell_result.get("clientOid"),
            "deposit_order_id": deposit_order_id
        }


class BitgetEarnFutures(BitgetEarn):
    """Bitget Futures trading with Earn integration 

    API Doc: https://www.bitget.com/api-doc/contract/intro
    """

    # Sleep times in seconds
    WAIT_AFTER_REDEEM_BEFORE_TRANSFER = 2
    WAIT_AFTER_SPOT_TO_FUTURES_TRANSFER = 2
    WAIT_AFTER_POSITION_EXIT_BEFORE_TRANSFER = 2
    WAIT_AFTER_FUTURES_TO_SPOT_TRANSFER = 2

    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        super().__init__(api_key, api_secret, passphrase)
        
        self._symbol_info = self._get_symbols_contracts()

    def _get_symbols_contracts(self) -> dict[str, dict]:
        """Get futures contract symbols info.
        
        API Doc: https://www.bitget.com/api-doc/contract/market/Get-All-Symbols-Contracts
        """
        data = self._request("GET", "/api/v2/mix/market/contracts", params={"productType": "USDT-FUTURES"})
        return {item["symbol"]: item for item in data}

    def _price_to_precision(self, symbol: str, price: float) -> str:
        """Adjust price to futures symbol precision using proper step logic."""
        price = float(price)
        contract_info = self._symbol_info[symbol]
        
        price_end_step = int(contract_info["priceEndStep"])
        price_decimals = int(contract_info["pricePlace"])
        
        if price_end_step == 1:
            return str(round(price, price_decimals))
        else:
            step_size = price_end_step / (10**price_decimals)
            price = int(price / step_size) * step_size
            return f"{price:.{price_decimals}f}"

    def _amount_to_precision(self, symbol: str, amount: float) -> str:
        """Adjust amount to futures symbol precision using proper contract logic."""
        amount = float(amount)
        contract_info = self._symbol_info[symbol]
        
        size_decimals = int(contract_info["volumePlace"])
        size_multiple_of = float(contract_info["sizeMultiplier"])
        min_trade_num = float(contract_info["minTradeNum"])
        
        if amount < min_trade_num:
            raise ValueError(f"Amount {amount} is below minimum trade amount {min_trade_num} for {symbol}")
        
        if size_decimals == 0:
            amount = int(amount // size_multiple_of * size_multiple_of)
            return str(amount)
        else:
            multiplier = 10**size_decimals
            amount = int(amount * multiplier) / multiplier
            return f"{amount:.{size_decimals}f}"

    def _convert_usdt_to_base_amount(self, symbol: str, usdt_amount: float) -> float:
        """Convert USDT amount to base currency amount for futures orders.
        
        For futures, we need to get the current mark price and convert USDT to base currency.
        """
        ticker_data = self._request(
            "GET",
            "/api/v2/mix/market/ticker",
            params={
                "symbol": symbol,
                "productType": "USDT-FUTURES"
            }
        )
        
        if not ticker_data or not ticker_data[0]:
            raise Exception(f"Could not get ticker data for {symbol}")
        
        mark_price = float(ticker_data[0]["markPrice"])
        base_amount = usdt_amount / mark_price
        return base_amount

    def _get_order_detail(self, symbol: str, order_id: str, client_oid: str | None = None) -> dict:
        """Get futures order info by ID.
        
        API Doc: https://www.bitget.com/api-doc/contract/trade/Get-Order-Details
        """
        params = {
            "symbol": symbol,
            "productType": "USDT-FUTURES",
            **{k: v for k, v in {
                "orderId": order_id,
                "clientOid": client_oid,
            }.items() if v is not None}
        }
            
        return self._request("GET", "/api/v2/mix/order/detail", params=params)

    def _get_position_mode(self, symbol: str, margin_coin: str = "USDT") -> str:
        """Get account position mode configuration to determine if tradeSide is needed.
        
        Uses the account endpoint which returns the actual account configuration,
        not just existing positions.
        """
        try:
            data = self._request(
                "GET",
                "/api/v2/mix/account/account",
                params={
                    "symbol": symbol,
                    "productType": "USDT-FUTURES",
                    "marginCoin": margin_coin
                }
            )
            
            if not data:
                raise Exception("No account data returned from API")
            
            pos_mode = data["posMode"]
            return pos_mode
        except Exception as e:
            raise Exception(f"Failed to get account position mode for {symbol}: {e}")

    def get_balance(self, coin: str = "USDT", *, product_type: str = "USDT-FUTURES") -> dict:
        """Get futures account balance for coin.
        
        API Doc: https://www.bitget.com/api-doc/contract/account/Get-Account-Assets
        """
        data = self._request(
            "GET",
            "/api/v2/mix/account/accounts",
            params={"coin": coin.upper(), "productType": product_type}
        )
        return data[0] if data else {}

    def transfer(
        self,
        from_type: str,
        to_type: str,
        amount: float,
        coin: str,
        *,
        symbol: str | None = None,
        client_oid: str | None = None
    ) -> dict:
        """Transfer funds between different product type accounts.
        
        API Doc: https://www.bitget.com/api-doc/spot/wallet/transfer
        """
        if from_type == "isolated_margin" or to_type == "isolated_margin":
            if symbol is None:
                raise ValueError("Symbol required for isolated margin transfers")
        
        body = {
            "fromType": from_type,
            "toType": to_type,
            "amount": str(amount),
            "coin": coin,
            **{k: v for k, v in {
                "symbol": symbol,
                "clientOid": client_oid,
            }.items() if v is not None}
        }
        
        return self._request("POST", "/api/v2/spot/wallet/transfer", body=body)

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float,
        *,
        product_type: str = "USDT-FUTURES",
        margin_mode: str = "isolated",
        margin_coin: str = "USDT",
        price: float | None = None,
        trade_side: str | None = None,
        force: str | None = None,
        client_oid: str | None = None,
        reduce_only: str | None = None,
        preset_stop_surplus_price: float | None = None,
        preset_stop_loss_price: float | None = None,
        preset_stop_surplus_execute_price: float | None = None,
        preset_stop_loss_execute_price: float | None = None,
        stp_mode: str | None = None
    ) -> dict:
        """Place futures order with automatic precision adjustment.
        
        API Doc: https://www.bitget.com/api-doc/contract/trade/Place-Order
        """
        if order_type == "limit" and price is None:
            raise ValueError("Price required for limit orders")
        
        if order_type == "limit" and force is None:
            force = "gtc"
        
        size_adjusted = self._amount_to_precision(symbol, size)
        price_adjusted = None
        if price is not None:
            price_adjusted = self._price_to_precision(symbol, price)
        
        body = {
            "symbol": symbol,
            "productType": product_type,
            "marginMode": margin_mode,
            "marginCoin": margin_coin,
            "size": size_adjusted,
            "side": side,
            "orderType": order_type,
            **{k: v for k, v in {
                "price": price_adjusted,
                "tradeSide": trade_side,
                "force": force,
                "clientOid": client_oid,
                "reduceOnly": reduce_only,
                "presetStopSurplusPrice": str(preset_stop_surplus_price) if preset_stop_surplus_price is not None else None,
                "presetStopLossPrice": str(preset_stop_loss_price) if preset_stop_loss_price is not None else None,
                "presetStopSurplusExecutePrice": str(preset_stop_surplus_execute_price) if preset_stop_surplus_execute_price is not None else None,
                "presetStopLossExecutePrice": str(preset_stop_loss_execute_price) if preset_stop_loss_execute_price is not None else None,
                "stpMode": stp_mode,
            }.items() if v is not None}
        }
        
        return self._request("POST", "/api/v2/mix/order/place-order", body=body)

    def enter_position_from_savings(
        self,
        symbol: str,
        margin_coin: str,
        amount: float,
        side: str,  # "long" or "short"
        *,
        order_type: str = "market",
        price: float | None = None,
        client_oid: str | None = None,
        force: str = "gtc",
        margin_mode: str = "isolated"
    ) -> dict:
        """Enter futures position using funds from savings.
        
        Flow: Redeem from savings → Transfer spot to futures → Open position
        
        Combines: Redeem-Savings + Transfer + Place-Order
        """
        print(f"Redeeming {amount} {margin_coin} from savings...")
        
        redeem_order_id = self.redeem_savings(margin_coin, amount)
        print(f"Redemption complete. Order ID: {redeem_order_id}")
        
        print(f"Waiting {self.WAIT_AFTER_REDEEM_BEFORE_TRANSFER} seconds after redeem...")
        time.sleep(self.WAIT_AFTER_REDEEM_BEFORE_TRANSFER)
        
        print(f"Transferring {amount} {margin_coin} from spot to futures...")
        transfer_result = self.transfer(
            from_type="spot",
            to_type="usdt_futures",
            amount=amount,
            coin=margin_coin
        )
        print("Transfer to futures complete.")
        
        print(f"Waiting {self.WAIT_AFTER_SPOT_TO_FUTURES_TRANSFER} seconds after transfer...")
        time.sleep(self.WAIT_AFTER_SPOT_TO_FUTURES_TRANSFER)
        
        print(f"Opening {side} position for {symbol}...")
        
        if side == "long":
            futures_side = "buy"
        elif side == "short":
            futures_side = "sell"
        else:
            raise ValueError(f"Invalid side: {side}. Must be 'long' or 'short'")
        
        base_amount = self._convert_usdt_to_base_amount(symbol, amount)
        
        position_mode = self._get_position_mode(symbol, margin_coin)
        
        if position_mode == "hedge_mode":
            position_result = self.place_order(
                symbol=symbol,
                side=futures_side,
                order_type=order_type,
                size=base_amount,
                price=price,
                client_oid=client_oid,
                force=force,
                margin_mode=margin_mode,
                margin_coin=margin_coin,
                trade_side="open"
            )
        else:
            position_result = self.place_order(
                symbol=symbol,
                side=futures_side,
                order_type=order_type,
                size=base_amount,
                price=price,
                client_oid=client_oid,
                force=force,
                margin_mode=margin_mode,
                margin_coin=margin_coin
            )
              
        print("Position opened.")

        return {
            "redeem_order_id": redeem_order_id,
            "transfer_result": transfer_result,
            "position_order_id": position_result["orderId"],
            "position_client_oid": position_result.get("clientOid")
        }

    def exit_position_to_savings(
        self,
        symbol: str,
        size: float,
        side: str,  # "long" or "short" 
        margin_coin: str = "USDT",
        *,
        order_type: str = "market",
        price: float | None = None,
        client_oid: str | None = None,
        force: str = "gtc",
        margin_mode: str = "isolated"
    ) -> dict:
        """Exit futures position and deposit proceeds to savings.
        
        Flow: Close position → Transfer futures to spot → Deposit to savings
        
        Combines: Place-Order + Transfer + Subscribe-Savings
        """
        print(f"Closing {side} position for {size} {symbol}...")
        
        if side == "long":
            futures_side = "buy"   
        elif side == "short":
            futures_side = "sell"  
        else:
            raise ValueError(f"Invalid side: {side}. Must be 'long' or 'short'")
        
        position_mode = self._get_position_mode(symbol, margin_coin)
        
        if position_mode == "hedge_mode":
            close_result = self.place_order(
                symbol=symbol,
                side=futures_side,
                order_type=order_type,
                size=size,
                price=price,
                client_oid=client_oid,
                force=force,
                margin_mode=margin_mode,
                margin_coin=margin_coin,
                trade_side="close",
                reduce_only="YES"
            )
        else:
            close_result = self.place_order(
                symbol=symbol,
                side=futures_side,
                order_type=order_type,
                size=size,
                price=price,
                client_oid=client_oid,
                force=force,
                margin_mode=margin_mode,
                margin_coin=margin_coin,
                reduce_only="YES"
            )
        
        print("Position closed.")
        
        print(f"Waiting {self.WAIT_AFTER_POSITION_EXIT_BEFORE_TRANSFER} seconds after position exit...")
        time.sleep(self.WAIT_AFTER_POSITION_EXIT_BEFORE_TRANSFER)
        
        futures_balance = self.get_balance(margin_coin)
        available_amount = futures_balance.get('available', '0')
        
        if float(available_amount) > 0:
            print(f"Transferring {available_amount} {margin_coin} from futures to spot...")
            transfer_result = self.transfer(
                from_type="usdt_futures",
                to_type="spot", 
                amount=available_amount,
                coin=margin_coin
            )
            print("Transfer to spot complete.")
            
            print(f"Waiting {self.WAIT_AFTER_FUTURES_TO_SPOT_TRANSFER} seconds after transfer...")
            time.sleep(self.WAIT_AFTER_FUTURES_TO_SPOT_TRANSFER)
            
            print(f"Depositing {available_amount} {margin_coin} to savings...")
            deposit_order_id = self.subscribe_savings(margin_coin, available_amount)
            print(f"Savings deposit complete. Order ID: {deposit_order_id}")
        else:
            print(f"No {margin_coin} available to transfer")
            transfer_result = None
            deposit_order_id = None
        
        return {
            "close_order_id": close_result["orderId"],
            "close_client_oid": close_result.get("clientOid"),
            "transfer_result": transfer_result,
            "deposit_order_id": deposit_order_id
        }
        
        