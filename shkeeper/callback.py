import click
from shkeeper import requests

from flask import Blueprint
from flask import current_app as app

from shkeeper.modules.classes.crypto import Crypto
from shkeeper.models import *


bp = Blueprint('callback', __name__)

def send_unconfirmed_notification(utx: UnconfirmedTransaction):
    app.logger.info(f'send_unconfirmed_notification started for {utx.crypto} {utx.txid}, {utx.addr}, {utx.amount_crypto}')

    invoice_address = InvoiceAddress.query.filter_by(crypto=utx.crypto, addr=utx.addr).first()
    invoice = Invoice.query.filter_by(id=invoice_address.invoice_id).first()
    crypto = Crypto.instances[utx.crypto]
    apikey = crypto.wallet.apikey

    notification = {
        "status": "unconfirmed",
        "external_id": invoice.external_id,
        "crypto": utx.crypto,
        "addr": utx.addr,
        "txid": utx.txid,
        "amount": format_decimal(utx.amount_crypto, precision=crypto.precision),
    }

    app.logger.warning(f'[{utx.crypto}/{utx.txid}] Posting {notification} to {invoice.callback_url} with api key {apikey}')
    try:
        r = requests.post(
            invoice.callback_url,
            json=notification,
            headers={"X-Shkeeper-Api-Key": apikey},
            timeout=app.config.get('REQUESTS_NOTIFICATION_TIMEOUT'),
        )
    except Exception as e:
        app.logger.error(f'[{utx.crypto}/{utx.txid}] Unconfirmed TX notification failed: {e}')
        return False

    if r.status_code != 202:
        app.logger.warning(f'[{utx.crypto}/{utx.txid}] Unconfirmed TX notification failed with HTTP code {r.status_code}')
        return False

    utx.callback_confirmed = True
    db.session.commit()
    app.logger.info(f'[{utx.crypto}/{utx.txid}] Unconfirmed TX notification has been accepted')

    return True

def send_notification(tx):
    app.logger.info(f'[{tx.crypto}/{tx.txid}] Notificator started')

    transactions = []
    for t in tx.invoice.transactions:
        transactions.append({
          "txid": t.txid,
          "date": str(t.created_at),
          "amount_crypto": str(round(t.amount_crypto.normalize(), 8)),
          "amount_fiat": str(round(t.amount_fiat.normalize(), 2)),
          "trigger": tx.id == t.id,
          "crypto": t.crypto,
        })

    notification = {
        "external_id": tx.invoice.external_id,
        "crypto": tx.invoice.crypto,
        "addr": tx.invoice.addr,
        "fiat": tx.invoice.fiat,
        "balance_fiat": str(round(tx.invoice.balance_fiat.normalize(), 2)),
        "balance_crypto": str(round(tx.invoice.balance_crypto.normalize(), 8)),
        "paid": tx.invoice.status in (InvoiceStatus.PAID, InvoiceStatus.OVERPAID),
        "status": tx.invoice.status.name,
        "transactions": transactions,
        "fee_percent": str(tx.invoice.rate.fee.normalize()),
    }

    overpaid_fiat = tx.invoice.balance_fiat - (tx.invoice.amount_fiat * ( tx.invoice.wallet.ulimit / 100))
    notification['overpaid_fiat'] = str(round(overpaid_fiat.normalize(), 2)) if overpaid_fiat > 0 else "0.00"

    apikey = Crypto.instances[tx.crypto].wallet.apikey
    app.logger.warning(f'[{tx.crypto}/{tx.txid}] Posting {notification} to {tx.invoice.callback_url} with api key {apikey}')
    try:
        r = requests.post(
            tx.invoice.callback_url,
            json=notification,
            headers={"X-Shkeeper-Api-Key": apikey},
            timeout=app.config.get('REQUESTS_NOTIFICATION_TIMEOUT'),
        )
    except Exception as e:
        app.logger.error(f'[{tx.crypto}/{tx.txid}] Notification failed: {e}')
        return False

    if r.status_code != 202:
        app.logger.warning(f'[{tx.crypto}/{tx.txid}] Notification failed by {tx.invoice.callback_url} with HTTP code {r.status_code}')
        return False

    tx.callback_confirmed = True
    db.session.commit()
    app.logger.info(f'[{tx.crypto}/{tx.txid}] Notification has been accepted by {tx.invoice.callback_url}')
    return True

def send_expired_notification(utxs):
    for utx in utxs:
        app.logger.info(f'[{utx.crypto}/{utx.external_id}] Expired Notificator started')

        notification = {
            "external_id": utx.external_id,
            "crypto": utx.crypto,
            "addr": utx.addr,
            "status": utx.status.name
        }

        apikey = Crypto.instances[utx.crypto].wallet.apikey
        app.logger.warning(f'[{utx.crypto}/{utx.external_id}] Posting {notification} to {utx.callback_url} with api key {apikey}')

        try:
            r = requests.post(
                utx.callback_url,
                json=notification,
                headers={"X-Shkeeper-Api-Key": apikey},
                timeout=app.config.get('REQUESTS_NOTIFICATION_TIMEOUT'),
            )
        except Exception as e:
            app.logger.error(f'[{utx.crypto}/{utx.external_id}] Notification failed: {e}')
            return False

        if r.status_code != 202:
            app.logger.warning(f'[{utx.crypto}/{utx.external_id}] Notification failed by {utx.callback_url} with HTTP code {r.status_code}')
            return False

        utx.callback_confirmed = True
        db.session.commit()
        app.logger.info(f'[{utx.crypto}/{utx.external_id}] Notification has been accepted by {utx.callback_url}')

    return True

def list_unconfirmed():
    for tx in Transaction.query.filter_by(callback_confirmed=False):
        print(tx)
    else:
        print("No unconfirmed transactions found!")

def send_callbacks():
    for utx in UnconfirmedTransaction.query.filter_by(callback_confirmed=False):
        send_unconfirmed_notification(utx)

    for tx in Transaction.query.filter_by(callback_confirmed=False, need_more_confirmations=False):
        if tx.invoice.status == InvoiceStatus.OUTGOING:
            tx.callback_confirmed = True
            db.session.commit()
        else:
            app.logger.info(f'[{tx.crypto}/{tx.txid}] Notification is pending')
            send_notification(tx)

def update_confirmations():
    for tx in Transaction.query.filter_by(callback_confirmed=False, need_more_confirmations=True):
        app.logger.info(f'[{tx.crypto}/{tx.txid}] Updating confirmations')
        if not tx.is_more_confirmations_needed():
            app.logger.info(f'[{tx.crypto}/{tx.txid}] Got enough confirmations')
            send_notification(tx)
        else:
            app.logger.info(f'[{tx.crypto}/{tx.txid}] Not enough confirmations yet')

    expired_invoices = []
    for itx in Invoice.query.filter_by(status=InvoiceStatus.UNPAID):
        app.logger.info(f'[{itx.crypto}/{itx.external_id}/{itx.wallet.recalc}] Updating status')
        if itx.wallet.recalc > 0:
            if (itx.created_at + timedelta(hours=itx.wallet.recalc)) < datetime.now():
                app.logger.info(f'[{itx.crypto}/{itx.external_id}] Updating status: expired')
                itx.status = InvoiceStatus.EXPIRED
                utx = Transaction.add(Crypto.instances[itx.crypto], {
                    "txid": "",
                    "addr": itx.addr,
                    "amount": 0,
                    "confirmations": 0
                })
                utx.callback_confirmed = True
                expired_invoices.append(itx)
                db.session.commit()

    send_expired_notification(expired_invoices)

@bp.cli.command()
def list():
    """Shows list of transaction notifications to be sent"""
    list_unconfirmed()

@bp.cli.command()
def send():
    """Send transaction notification"""
    send_callbacks()

@bp.cli.command()
def update():
    """Update number of confirmation"""
    update_confirmations()

@bp.cli.command()
@click.option('-c', '--confirmations', default=1)
def add(confirmations):
        import time
        crypto = Crypto.instances['BTC']
        invoice = Invoice.add(crypto, {
            "external_id": str(time.time()),
            "fiat": "USD",
            "amount": 1000,
            "callback_url": "http://localhost:5000/api/v1/wp_callback",
        })
        tx = Transaction.add(crypto, {
            "txid": invoice.id * 100,
            "addr": invoice.addr,
            "amount": invoice.amount_crypto,
            "confirmations": confirmations
        })
