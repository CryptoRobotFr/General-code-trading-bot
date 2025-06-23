import base64
import hmac
import json
import time
from urllib.parse import urlencode

import requests


class BitgetClient:
    """Client Bitget pour les opérations Spot et Earn."""
    
    def __init__(self, api_key: str, api_secret: str, passphrase: str, api_host: str = "https://api.bitget.com"):
        """
        Initialise le client Bitget avec les credentials API.
        
        Paramètres
        ----------
        api_key : str
            Clé API Bitget
        api_secret : str
            Secret API Bitget  
        passphrase : str
            Passphrase API Bitget
        api_host : str, optionnel
            URL de base de l'API Bitget
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.api_host = api_host
    
    def _timestamp_ms(self) -> str:
        """Timestamp actuel en millisecondes (format string selon spec Bitget)."""
        return str(int(time.time() * 1000))
    
    def _sign(self, ts: str, method: str, path: str, query: str = "", body: str = "") -> str:
        """
        Retourne la signature HMAC-SHA256 encodée en Base64 pour la requête.
        """
        prehash = f"{ts}{method.upper()}{path}{query}{body}"
        digest = hmac.new(self.api_secret.encode(), prehash.encode(), digestmod="sha256").digest()
        return base64.b64encode(digest).decode()
    
    def _headers(self, ts: str, signature: str) -> dict:
        """En-têtes HTTP requis par Bitget."""
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
    
    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None):
        """
        Requête REST générique Bitget avec signature et gestion d'erreurs de base.
        Retourne le champ `data` de la réponse JSON.
        """
        params = params or {}
        body = body or {}
        query = "?" + urlencode(sorted(params.items())) if params else ""
        body_s = json.dumps(body) if body else ""

        ts = self._timestamp_ms()
        sign = self._sign(ts, method, path, query, body_s)
        url = f"{self.api_host}{path}{query}"

        r = requests.request(method, url, headers=self._headers(ts, sign), data=body_s, timeout=10)
        
        # Debug: print the response if there's an error
        if r.status_code != 200:
            print(f"❌ HTTP {r.status_code} Error Response: {r.text}")
        
        r.raise_for_status()

        payload = r.json()
        if payload.get("code") != "00000":
            print(f"❌ API Error Response: {payload}")
            raise RuntimeError(f"Erreur API Bitget: {payload}")

        return payload["data"]
    
    def _current_usdt_flexible_product_id(self) -> str:
        """
        Retourne le productId d'un produit d'épargne flexible USDT *actif*.
        Lève une exception si aucun n'est disponible.
        """
        products = self._request(
            "GET",
            "/api/v2/earn/savings/product",
            params={"coin": "USDT", "filter": "available_and_held"}
        )
        for p in products:
            if p["periodType"] == "flexible" and p["status"] == "in_progress":
                return p["productId"]

        raise ValueError("Aucun produit d'épargne flexible USDT actif trouvé (épuisé ?)")
    
    def subscribe_savings_usdt(self, amount_usdt: str) -> str:
        """
        Dépose `amount_usdt` (string, jusqu'à 8 décimales) dans l'épargne flexible USDT.
        Retourne l'orderId résultant.
        """
        pid = self._current_usdt_flexible_product_id()
        result = self._request(
            "POST",
            "/api/v2/earn/savings/subscribe",
            body={
                "periodType": "flexible",
                "productId": pid,
                "amount": amount_usdt
            }
        )
        return result["orderId"]
    
    def redeem_savings_usdt(self, amount_usdt: str) -> str:
        """
        Retire `amount_usdt` d'USDT de l'épargne flexible vers le Spot.
        Bitget impose au moins 1 minute entre les retraits du même produit.
        Retourne l'orderId résultant.
        """
        pid = self._current_usdt_flexible_product_id()
        result = self._request(
            "POST",
            "/api/v2/earn/savings/redeem",
            body={
                "periodType": "flexible",
                "productId": pid,
                "amount": amount_usdt
            }
        )
        return result["orderId"]
    
    def get_spot_balance(self, coin: str = "USDT", *, asset_type: str = "hold_only") -> dict:
        """
        Retourne le solde de votre portefeuille Spot pour `coin`.

        Paramètres
        ----------
        coin : str
            Symbole du token, ex. "USDT", "BTC". Par défaut "USDT".
        asset_type : str
            "hold_only" (défaut) ou "all". Correspond au paramètre `assetType` de Bitget.

        Retour
        ------
        dict
            Exemple:
            {
              "coin": "USDT",
              "available": "37.221645",
              "frozen": "0",
              "locked": "0",
              "limitAvailable": "0",
              "uTime": "1718081856000"
            }
        """
        data = self._request(
            "GET",
            "/api/v2/spot/account/assets",
            params={"coin": coin.upper(), "assetType": asset_type}
        )
        return data[0] if data else {}
    
    def _get_savings_assets(
        self,
        period_type: str,
        *,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: str | None = None,
        id_less_than: str | None = None
    ) -> dict:
        """
        Récupère les actifs d'épargne selon les critères spécifiés.

        Paramètres
        ----------
        period_type : str
            Type de période:
            - "flexible" : dépôt à vue
            - "fixed" : dépôt à terme
        start_time : str, optionnel
            Timestamp de début en millisecondes (ex: "1597026383085")
            Par défaut: il y a 3 mois
        end_time : str, optionnel
            Timestamp de fin en millisecondes (ex: "1597026383085")
            Par défaut: maintenant
        limit : str, optionnel
            Nombre d'éléments par page. Par défaut: "20", maximum: "100"
        id_less_than : str, optionnel
            ID de pagination pour requêter les données plus anciennes
        """
        params = {"periodType": period_type}
        
        if start_time is not None:
            params["startTime"] = start_time
        if end_time is not None:
            params["endTime"] = end_time
        if limit is not None:
            params["limit"] = limit
        if id_less_than is not None:
            params["idLessThan"] = id_less_than

        return self._request(
            "GET",
            "/api/v2/earn/savings/assets",
            params=params
        )
    
    def get_flexible_savings_amount(self, coin: str = "USDT") -> str:
        """
        Retourne le montant détenu en épargne flexible pour une crypto donnée.

        Paramètres
        ----------
        coin : str
            Symbole du token, ex. "USDT", "BTC". Par défaut "USDT".

        Retour
        ------
        str
            Montant détenu. "0" si aucun actif trouvé pour cette crypto.
        """
        data = self._get_savings_assets("flexible")
        
        for asset in data.get("resultList", []):
            if asset.get("productCoin", "").upper() == coin.upper():
                return asset.get("holdAmount", "0")
        
        return "0"
    
    def place_futures_order(
        self,
        symbol: str,
        product_type: str,
        margin_mode: str,
        margin_coin: str,
        size: str,
        side: str,
        order_type: str,
        *,
        price: str | None = None,
        trade_side: str | None = None,
        force: str | None = None,
        client_oid: str | None = None,
        reduce_only: str | None = None,
        preset_stop_surplus_price: str | None = None,
        preset_stop_loss_price: str | None = None,
        preset_stop_surplus_execute_price: str | None = None,
        preset_stop_loss_execute_price: str | None = None,
        stp_mode: str | None = None
    ) -> dict:
        """
        Place a futures/contract order on Bitget.

        Paramètres
        ----------
        symbol : str
            Trading pair, e.g. "ETHUSDT"
        product_type : str
            Product type:
            - "USDT-FUTURES": USDT-M Futures
            - "COIN-FUTURES": Coin-M Futures  
            - "USDC-FUTURES": USDC-M Futures
            - "SUSDT-FUTURES": USDT-M Futures Demo
            - "SCOIN-FUTURES": Coin-M Futures Demo
            - "SUSDC-FUTURES": USDC-M Futures Demo
        margin_mode : str
            Position mode: "isolated" or "crossed"
        margin_coin : str
            Margin coin (capitalized), e.g. "USDT"
        size : str
            Amount (base coin)
        side : str
            Trade side:
            - "buy": Buy(one-way-mode); Long position direction(hedge-mode)
            - "sell": Sell(one-way-mode); Short position direction(hedge-mode)
        order_type : str
            Order type: "limit" or "market"
        price : str, optionnel
            Price of the order. Required if order_type is "limit"
        trade_side : str, optionnel
            Trade type (only required in hedge-mode):
            - "open": Open position
            - "close": Close position
        force : str, optionnel
            Order expiration date (required if order_type is "limit"):
            - "ioc": Immediate or cancel
            - "fok": Fill or kill
            - "gtc": Good till canceled (default)
            - "post_only": Post only
        client_oid : str, optionnel
            Custom order ID
        reduce_only : str, optionnel
            Whether to just reduce position: "YES" or "NO" (default: "NO")
            Applicable only in one-way-position mode
        preset_stop_surplus_price : str, optionnel
            Take-profit value
        preset_stop_loss_price : str, optionnel
            Stop-loss value
        preset_stop_surplus_execute_price : str, optionnel
            Preset stop-profit execution price
        preset_stop_loss_execute_price : str, optionnel
            Preset stop-loss execution price
        stp_mode : str, optionnel
            STP Mode (Self Trade Prevention):
            - "none": not setting STP (default)
            - "cancel_taker": cancel taker order
            - "cancel_maker": cancel maker order
            - "cancel_both": cancel both taker and maker orders

        Retour
        ------
        dict
            Exemple:
            {
                "clientOid": "121211212122",
                "orderId": "121211212122"
            }
        
        Raises
        ------
        ValueError
            Si les paramètres requis ne sont pas fournis
        RuntimeError
            Si l'API Bitget retourne une erreur
        """
        try:
            if order_type == "limit" and price is None:
                raise ValueError("Le paramètre 'price' est requis pour les ordres limit")
            
            if order_type == "limit" and force is None:
                force = "gtc"  # Valeur par défaut
            
            body = {
                "symbol": symbol,
                "productType": product_type,
                "marginMode": margin_mode,
                "marginCoin": margin_coin,
                "size": size,
                "side": side,
                "orderType": order_type
            }
            
            if price is not None:
                body["price"] = price
            if trade_side is not None:
                body["tradeSide"] = trade_side
            if force is not None:
                body["force"] = force
            if client_oid is not None:
                body["clientOid"] = client_oid
            if reduce_only is not None:
                body["reduceOnly"] = reduce_only
            if preset_stop_surplus_price is not None:
                body["presetStopSurplusPrice"] = preset_stop_surplus_price
            if preset_stop_loss_price is not None:
                body["presetStopLossPrice"] = preset_stop_loss_price
            if preset_stop_surplus_execute_price is not None:
                body["presetStopSurplusExecutePrice"] = preset_stop_surplus_execute_price
            if preset_stop_loss_execute_price is not None:
                body["presetStopLossExecutePrice"] = preset_stop_loss_execute_price
            if stp_mode is not None:
                body["stpMode"] = stp_mode
            
            result = self._request(
                "POST",
                "/api/v2/mix/order/place-order",
                body=body
            )
            
            return result
            
        except Exception as e:
            print(f"❌ Erreur lors du placement de l'ordre: {e}")
            raise
    
    def sub_transfer(
        self,
        from_user_id: str,
        to_user_id: str,
        from_type: str,
        to_type: str,
        amount: str,
        coin: str,
        *,
        symbol: str | None = None,
        client_oid: str | None = None
    ) -> dict:
        """
        Transfer funds between sub-accounts or between parent and sub-accounts.
        
        Note: Only parent account API Key can use this endpoint, and the API Key must bind IP.

        Paramètres
        ----------
        from_user_id : str
            Outgoing Account UID
        to_user_id : str
            Incoming Account UID
        from_type : str
            Source account type:
            - "spot": Spot account
            - "p2p": P2P/Funding account
            - "coin_futures": Coin-M futures account
            - "usdt_futures": USDT-M futures account
            - "usdc_futures": USDC-M futures account
            - "crossed_margin": Cross margin account
            - "isolated_margin": Isolated margin account
        to_type : str
            Destination account type:
            - "spot": Spot account
            - "p2p": P2P/Funding account
            - "coin_futures": Coin-M futures account
            - "usdt_futures": USDT-M futures account
            - "usdc_futures": USDC-M futures account
            - "crossed_margin": Cross margin account
            - "isolated_margin": Isolated margin account
        amount : str
            Amount to transfer
        coin : str
            Currency of transfer (e.g., "USDT", "BTC")
        symbol : str, optionnel
            Symbol name (Required for Isolated margin (spot) transferring)
        client_oid : str, optionnel
            Custom order ID

        Retour
        ------
        dict
            Exemple:
            {
                "transferId": "123456",
                "clientOid": "x123"
            }
        
        Raises
        ------
        ValueError
            Si les paramètres requis ne sont pas fournis
        RuntimeError
            Si l'API Bitget retourne une erreur
        """
        if from_type == "isolated_margin" or to_type == "isolated_margin":
            if symbol is None:
                raise ValueError("Le paramètre 'symbol' est requis pour les transferts isolated margin")
        
        body = {
            "fromUserId": from_user_id,
            "toUserId": to_user_id,
            "fromType": from_type,
            "toType": to_type,
            "amount": amount,
            "coin": coin
        }
        
        if symbol is not None:
            body["symbol"] = symbol
        if client_oid is not None:
            body["clientOid"] = client_oid
        
        return self._request(
            "POST",
            "/api/v2/spot/wallet/subaccount-transfer",
            body=body
        )

    def place_spot_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        force: str,
        size: str,
        *,
        price: str | None = None,
        client_oid: str | None = None,
        trigger_price: str | None = None,
        tpsl_type: str | None = None,
        request_time: str | None = None,
        receive_window: str | None = None,
        stp_mode: str | None = None,
        preset_take_profit_price: str | None = None,
        execute_take_profit_price: str | None = None,
        preset_stop_loss_price: str | None = None,
        execute_stop_loss_price: str | None = None
    ) -> dict:
        """
        Place a spot order on Bitget.

        Paramètres
        ----------
        symbol : str
            Trading pair name, e.g. "BTCUSDT"
        side : str
            Order direction:
            - "buy": Buy
            - "sell": Sell
        order_type : str
            Order type:
            - "limit": Limit order
            - "market": Market order
        force : str
            Execution strategy (invalid when order_type is market):
            - "gtc": Normal limit order, good till cancelled
            - "post_only": Post only
            - "fok": Fill or kill
            - "ioc": Immediate or cancel
        size : str
            Amount:
            - For Limit and Market-Sell orders: number of base coins
            - For Market-Buy orders: number of quote coins
        price : str, optionnel
            Limit price (required for limit orders)
        client_oid : str, optionnel
            Custom order ID
        trigger_price : str, optionnel
            SPOT TP/SL trigger price (only required in SPOT TP/SL order)
        tpsl_type : str, optionnel
            Order type:
            - "normal": SPOT order (default)
            - "tpsl": SPOT TP/SL order
        request_time : str, optionnel
            Request time, Unix millisecond timestamp
        receive_window : str, optionnel
            Valid time window, Unix millisecond timestamp
        stp_mode : str, optionnel
            STP Mode (Self Trade Prevention):
            - "none": not setting STP (default)
            - "cancel_taker": cancel taker order
            - "cancel_maker": cancel maker order
            - "cancel_both": cancel both taker and maker orders
        preset_take_profit_price : str, optionnel
            Take profit price
        execute_take_profit_price : str, optionnel
            Take profit execute price
        preset_stop_loss_price : str, optionnel
            Stop loss price
        execute_stop_loss_price : str, optionnel
            Stop loss execute price

        Retour
        ------
        dict
            Exemple:
            {
                "orderId": "1001",
                "clientOid": "121211212122"
            }
        
        Raises
        ------
        ValueError
            Si les paramètres requis ne sont pas fournis
        RuntimeError
            Si l'API Bitget retourne une erreur
        """
        if order_type == "limit" and price is None:
            raise ValueError("Le paramètre 'price' est requis pour les ordres limit")
        
        body = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "size": size
        }
        
        # Force parameter is invalid for market orders
        if order_type == "limit":
            body["force"] = force
        
        if price is not None:
            body["price"] = price
        if client_oid is not None:
            body["clientOid"] = client_oid
        if trigger_price is not None:
            body["triggerPrice"] = trigger_price
        if tpsl_type is not None:
            body["tpslType"] = tpsl_type
        if request_time is not None:
            body["requestTime"] = request_time
        if receive_window is not None:
            body["receiveWindow"] = receive_window
        if stp_mode is not None:
            body["stpMode"] = stp_mode
        if preset_take_profit_price is not None:
            body["presetTakeProfitPrice"] = preset_take_profit_price
        if execute_take_profit_price is not None:
            body["executeTakeProfitPrice"] = execute_take_profit_price
        if preset_stop_loss_price is not None:
            body["presetStopLossPrice"] = preset_stop_loss_price
        if execute_stop_loss_price is not None:
            body["executeStopLossPrice"] = execute_stop_loss_price
        
        return self._request(
            "POST",
            "/api/v2/spot/trade/place-order",
            body=body
        )

    def get_spot_order_info(self, order_id: str, client_oid: str | None = None) -> dict:
        """
        Get spot order information by order ID or client order ID.
        
        Paramètres
        ----------
        order_id : str
            Order ID (required if client_oid not provided)
        client_oid : str, optionnel
            Client order ID (required if order_id not provided)
            
        Retour
        ------
        dict
            Order information including:
            {
                "orderId": "1001",
                "clientOid": "custom123",
                "symbol": "BTCUSDT",
                "side": "buy",
                "orderType": "market",
                "status": "filled",
                "baseSize": "0.001",
                "quoteSize": "23.45",
                "fillPrice": "23450.00",
                "fillSize": "0.001",
                "fillTotalAmount": "23.45",
                ...
            }
        """
        params = {}
        if order_id:
            params["orderId"] = order_id
        if client_oid:
            params["clientOid"] = client_oid
            
        return self._request(
            "GET",
            "/api/v2/spot/trade/orderInfo",
            params=params
        )

    def get_symbol_info(self) -> list[dict]:
        """
        Get all spot trading symbols and their information.
        
        Returns the symbol information including price precision, quantity precision,
        minimum order size, etc.
        
        Retour
        ------
        list[dict]
            Liste des symboles avec leurs informations:
            [
                {
                    "symbol": "BTCUSDT",
                    "baseCoin": "BTC",
                    "quoteCoin": "USDT",
                    "minTradeAmount": "0.00001",
                    "maxTradeAmount": "10000000",
                    "takerFeeRate": "0.001",
                    "makerFeeRate": "0.001",
                    "priceScale": "2",
                    "quantityScale": "5",
                    "status": "online"
                },
                ...
            ]
        """
        return self._request(
            "GET",
            "/api/v2/spot/public/symbols"
        )


def price_to_precision_spot(client: BitgetClient, symbol: str, price: float | str) -> str:
    """
    Ajuste un prix à la précision requise pour un symbole spot donné.
    
    Paramètres
    ----------
    client : BitgetClient
        Instance du client Bitget
    symbol : str
        Symbole de trading, ex. "BTCUSDT"
    price : float | str
        Prix à ajuster
        
    Retour
    ------
    str
        Prix ajusté à la bonne précision
        
    Raises
    ------
    ValueError
        Si le symbole n'est pas trouvé
    """
    symbols_info = client.get_symbol_info()
    
    for symbol_info in symbols_info:
        if symbol_info["symbol"] == symbol.upper():
            price_precision = int(symbol_info["pricePrecision"])
            price_float = float(price)
            
            # Arrondir à la précision requise (pour les prix)
            return f"{price_float:.{price_precision}f}"
    
    raise ValueError(f"Symbole {symbol} non trouvé")


def amount_to_precision_spot(client: BitgetClient, symbol: str, amount: float | str) -> str:
    """
    Ajuste une quantité à la précision requise pour un symbole spot donné.
    
    Paramètres
    ----------
    client : BitgetClient
        Instance du client Bitget
    symbol : str
        Symbole de trading, ex. "BTCUSDT"
    amount : float | str
        Quantité à ajuster
        
    Retour
    ------
    str
        Quantité ajustée à la bonne précision
        
    Raises
    ------
    ValueError
        Si le symbole n'est pas trouvé
    """
    symbols_info = client.get_symbol_info()
    
    for symbol_info in symbols_info:
        if symbol_info["symbol"] == symbol.upper():
            quantity_precision = int(symbol_info["quantityPrecision"])
            amount_float = float(amount)
            
            # Tronquer à la précision requise (pour les quantités)
            multiplier = 10 ** quantity_precision
            truncated_amount = int(amount_float * multiplier) / multiplier
            
            return f"{truncated_amount:.{quantity_precision}f}"
    
    raise ValueError(f"Symbole {symbol} non trouvé")


def place_spot_buy_order_from_savings(
    client: BitgetClient,
    symbol: str,
    amount_usdt: str,
    *,
    order_type: str = "market",
    price: str | None = None,
    client_oid: str | None = None,
    force: str = "gtc"
) -> dict:
    """
    Retire des USDT de l'épargne flexible et place un ordre d'achat spot.
    
    Cette fonction combine deux opérations:
    1. Retrait de `amount_usdt` USDT de l'épargne flexible vers le spot
    2. Placement d'un ordre d'achat spot avec ces USDT
    
    Paramètres
    ----------
    client : BitgetClient
        Instance du client Bitget
    symbol : str
        Paire de trading, ex. "BTCUSDT"
    amount_usdt : str
        Montant en USDT à retirer de l'épargne et utiliser pour l'achat
    order_type : str, optionnel
        Type d'ordre: "market" (défaut) ou "limit"
    force : str, optionnel
        Stratégie d'exécution: "gtc" (défaut), "post_only", "fok", "ioc"
    price : str, optionnel
        Prix limite (requis pour les ordres limit)
    client_oid : str, optionnel
        ID d'ordre personnalisé
        
    Retour
    ------
    dict
        {
            "redeem_order_id": "...",  
            "buy_order_id": "...",    
            "buy_client_oid": "..." 
        }
        
    Raises
    ------
    ValueError
        Si les paramètres sont invalides
    RuntimeError
        Si une des opérations API échoue
    """
    print(f"Retrait de {amount_usdt} USDT de l'épargne flexible...")
    
    redeem_order_id = client.redeem_savings_usdt(amount_usdt)
    print(f"Retrait effectué. Order ID: {redeem_order_id}")
    
    print("Attente de 2 secondes pour la disponibilité des fonds...")
    time.sleep(2)
    
    print(f"Placement d'un ordre d'achat {order_type} pour {symbol}...")
    
    size_adjusted = amount_to_precision_spot(client, symbol, amount_usdt)
    price_adjusted = None
    if price is not None:
        price_adjusted = price_to_precision_spot(client, symbol, price)
    
    buy_result = client.place_spot_order(
        symbol=symbol,
        side="buy",
        order_type=order_type,
        force=force,
        size=size_adjusted,
        price=price_adjusted,
        client_oid=client_oid
    )
    
    print(f"Ordre d'achat placé. Order ID: {buy_result['orderId']}")
    
    return {
        "redeem_order_id": redeem_order_id,
        "buy_order_id": buy_result["orderId"],
        "buy_client_oid": buy_result.get("clientOid")
    }


def place_spot_sell_order_to_savings(
    client: BitgetClient,
    symbol: str,
    amount_base: str,
    *,
    order_type: str = "market",
    price: str | None = None,
    client_oid: str | None = None,
    force: str = "gtc",
) -> dict:
    """
    Place un ordre de vente spot et optionnellement dépose les USDT obtenus en épargne.
    
    Cette fonction combine potentiellement deux opérations:
    1. Placement d'un ordre de vente spot
    2. (Optionnel) Dépôt des USDT obtenus en épargne flexible
    
    Paramètres
    ----------
    client : BitgetClient
        Instance du client Bitget
    symbol : str
        Paire de trading, ex. "BTCUSDT"
    amount_base : str
        Montant en coin de base à vendre (ex: montant de BTC pour BTCUSDT)
    order_type : str, optionnel
        Type d'ordre: "market" (défaut) ou "limit"
    force : str, optionnel
        Stratégie d'exécution: "gtc" (défaut), "post_only", "fok", "ioc"
    price : str, optionnel
        Prix limite (requis pour les ordres limit)
    client_oid : str, optionnel
        ID d'ordre personnalisé
        
    Retour
    ------
    dict
        Résultat contenant:
        {
            "sell_order_id": "...",        # ID de l'ordre de vente
            "sell_client_oid": "...",      # Client OID de l'ordre de vente
            "deposit_order_id": "..."      # ID du dépôt en épargne
        }
        
    Raises
    ------
    ValueError
        Si les paramètres sont invalides
    RuntimeError
        Si une des opérations API échoue
    """
    print(f"Placement d'un ordre de vente {order_type} de {amount_base} pour {symbol}...")
    
    size_adjusted = amount_to_precision_spot(client, symbol, amount_base)
    price_adjusted = None
    if price is not None:
        price_adjusted = price_to_precision_spot(client, symbol, price)
    
    sell_result = client.place_spot_order(
        symbol=symbol,
        side="sell",
        order_type=order_type,
        force=force,
        size=size_adjusted,
        price=price_adjusted,
        client_oid=client_oid
    )
    
    print(f"Ordre de vente placé. Order ID: {sell_result['orderId']}")
    
    print("Attente de 5 secondes avant le dépôt en épargne...")
    time.sleep(5)
    
    order_info_list = client.get_spot_order_info(sell_result['orderId'])
    
    if order_info_list and len(order_info_list) > 0:
        order_info = order_info_list[0]
        
        if order_info.get('status') == 'filled':
            usdt_received = order_info.get('quoteVolume', '0')
            
            if float(usdt_received) > 0:
                print(f"Dépôt de {usdt_received} USDT (provenant de la vente) en épargne flexible...")
                usdt_adjusted = amount_to_precision_spot(client, symbol, usdt_received)
                deposit_order_id = client.subscribe_savings_usdt(usdt_adjusted)
                print(f"Dépôt en épargne effectué. Order ID: {deposit_order_id}")
            else:
                print("Aucun USDT reçu de la vente")
                deposit_order_id = None
        else:
            print(f"Ordre non exécuté (status: {order_info.get('status', 'unknown')})")
            deposit_order_id = None
    else:
        print("Impossible de récupérer les informations de l'ordre")
        deposit_order_id = None
    
    result = {
        "sell_order_id": sell_result["orderId"],
        "sell_client_oid": sell_result.get("clientOid"),
        "deposit_order_id": deposit_order_id
    }
    
    return result


def test_connection(client: BitgetClient):
    """Test de connexion à l'API Bitget."""
    try:
        balance = client.get_spot_balance("USDT")
        
        if balance:
            print("🎉 Connexion API réussie !")
            print(f"Solde USDT disponible : {balance['available']} USDT")
            print(f"USDT gelés : {balance['frozen']} USDT")
            print(f"USDT verrouillés : {balance['locked']} USDT")
        else:
            print("⚠️  Aucun solde USDT trouvé dans le portefeuille Spot")
            
    except Exception as e:
        print(f"❌ Erreur lors de la récupération du solde : {e}")
        print("3Vérifiez vos clés API")
        

def main():
    """Fonction principale pour tester le client et les fonctions combo."""
    API_KEY=""
    API_SECRET=""
    PASSPHRASE=""
       
    client = BitgetClient(API_KEY, API_SECRET, PASSPHRASE)
    
    print("🔗 Test de connexion à l'API Bitget")
    print("="*50)
    test_connection(client)
    
    test_amount_usdt = "10"  
    test_symbol = "BTCUSDT" 
    
    try:
        print(f"\n🎯 Début du test complet avec {test_amount_usdt} USDT sur {test_symbol}")
        print("="*70)
        
        print(f"\n1️⃣  ÉTAPE 1: Dépôt de {test_amount_usdt} USDT en épargne flexible")
        print("-" * 50)
        
        deposit_order_id = client.subscribe_savings_usdt(test_amount_usdt)
        print(f"Dépôt effectué. Order ID: {deposit_order_id}")
        
        print("Attente de 3 secondes pour le traitement du dépôt...")
        time.sleep(3)
        
        savings_amount = client.get_flexible_savings_amount("USDT")
        print(f"Montant total en épargne flexible: {savings_amount} USDT")
        
        print(f"\n2️⃣  ÉTAPE 2: Achat de {test_symbol} avec {test_amount_usdt} USDT de l'épargne")
        print("-" * 50)
        
        place_spot_buy_order_from_savings(
            client=client,
            symbol=test_symbol,
            amount_usdt=test_amount_usdt,
            order_type="market"
        )
        
        print(f"Achat terminé")
        
        usdt_balance = client.get_spot_balance("USDT").get('available', '0')
        btc_balance = client.get_spot_balance("BTC").get('available', '0')
        print(f"Solde USDT après achat: {usdt_balance} USDT")
        print(f"Solde BTC après achat: {btc_balance} BTC")
        
        print("\nAttente de 5 secondes avant la vente...")
        time.sleep(5)
        
        print(f"\n3️⃣  ÉTAPE 3: Vente du BTC et retour en épargne")
        print("-" * 50)
        
        place_spot_sell_order_to_savings(
            client=client,
            symbol=test_symbol,
            amount_base=btc_balance,
            order_type="market"
        )
        
        print("\nAttente de 5 secondes avant la vérification finale...")
        time.sleep(5)
        
        print(f"\n4️⃣  VÉRIFICATION FINALE")
        print("-" * 50)
        
        final_usdt_balance = client.get_spot_balance("USDT")
        final_btc_balance = client.get_spot_balance("BTC")
        final_savings_amount = client.get_flexible_savings_amount("USDT")
        
        print(f"Solde USDT final: {final_usdt_balance.get('available', '0')} USDT")
        print(f"Solde BTC final: {final_btc_balance.get('available', '0')} BTC")
        print(f"Épargne USDT finale: {final_savings_amount} USDT")
        
        print(f"\n🎉 Test complet terminé avec succès!")
        print("="*70)
        
    except Exception as e:
        print(f"\n❌ Erreur durant le test: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main() 