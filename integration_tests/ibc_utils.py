import hashlib
import json
import subprocess
from contextlib import contextmanager
from enum import IntEnum
from pathlib import Path
from typing import NamedTuple

import requests
from pystarport import cluster, ports

from .network import Chainmain, Cronos, Hermes, setup_custom_cronos
from .utils import (
    ADDRS,
    CONTRACTS,
    deploy_contract,
    derive_new_account,
    eth_to_bech32,
    find_log_event_attrs,
    parse_events_rpc,
    send_transaction,
    setup_token_mapping,
    wait_for_fn,
    wait_for_new_blocks,
    wait_for_port,
)

RATIO = 10**10
RELAYER_CALLER = "0x6F1805D56bF05b7be10857F376A5b1c160C8f72C"


class Status(IntEnum):
    PENDING, SUCCESS, FAIL = range(3)


class IBCNetwork(NamedTuple):
    cronos: Cronos
    chainmain: Chainmain
    hermes: Hermes | None
    incentivized: bool


def call_hermes_cmd(
    hermes,
    connection_only,
    incentivized,
    version,
):
    if connection_only:
        subprocess.check_call(
            [
                "hermes",
                "--config",
                hermes.configpath,
                "create",
                "connection",
                "--a-chain",
                "cronos_777-1",
                "--b-chain",
                "chainmain-1",
            ]
        )
    else:
        subprocess.check_call(
            [
                "hermes",
                "--config",
                hermes.configpath,
                "create",
                "channel",
                "--a-port",
                "transfer",
                "--b-port",
                "transfer",
                "--a-chain",
                "cronos_777-1",
                "--b-chain",
                "chainmain-1",
                "--new-client-connection",
                "--yes",
            ]
            + (
                [
                    "--channel-version",
                    json.dumps(version),
                ]
                if incentivized
                else []
            )
        )


def call_rly_cmd(path, connection_only, version, hostchain="chainmain-1"):
    cmd = [
        "rly",
        "pth",
        "new",
        hostchain,
        "cronos_777-1",
        "chainmain-cronos",
        "--home",
        str(path),
    ]
    subprocess.check_call(cmd)
    if connection_only:
        cmd = [
            "rly",
            "tx",
            "connect",
            "chainmain-cronos",
            "--home",
            str(path),
        ]
    else:
        cmd = [
            "rly",
            "tx",
            "connect",
            "chainmain-cronos",
            "--src-port",
            "transfer",
            "--dst-port",
            "transfer",
            "--order",
            "unordered",
            "--version",
            json.dumps(version),
            "--home",
            str(path),
        ]
    subprocess.check_call(cmd)


def prepare_network(
    tmp_path,
    file,
    incentivized=True,
    is_relay=True,
    connection_only=False,
    grantee=None,
    relayer=cluster.Relayer.HERMES.value,
):
    print("incentivized", incentivized)
    print("is_relay", is_relay)
    print("connection_only", connection_only)
    print("relayer", relayer)
    is_hermes = relayer == cluster.Relayer.HERMES.value
    hermes = None
    file = f"configs/{file}.jsonnet"
    with contextmanager(setup_custom_cronos)(
        tmp_path,
        26700,
        Path(__file__).parent / file,
        relayer=relayer,
    ) as cronos:
        if grantee:
            cli = cronos.cosmos_cli()
            granter_addr = cli.address("signer1")
            grantee_addr = cli.address(grantee)
            max_gas = 1000000
            gas_price = 10000000000000000
            limit = f"{max_gas*gas_price*2}basetcro"
            rsp = cli.grant(granter_addr, grantee_addr, limit)
            assert rsp["code"] == 0, rsp["raw_log"]
            grant_detail = cli.query_grant(granter_addr, grantee_addr)
            assert grant_detail["granter"] == granter_addr
            assert grant_detail["grantee"] == grantee_addr

        chainmain = Chainmain(cronos.base_dir.parent / "chainmain-1")
        # wait for grpc ready
        wait_for_port(ports.grpc_port(chainmain.base_port(0)))  # chainmain grpc
        wait_for_port(ports.grpc_port(cronos.base_port(0)))  # cronos grpc

        version = {"fee_version": "ics29-1", "app_version": "ics20-1"}
        path = cronos.base_dir.parent / "relayer"
        if is_hermes:
            hermes = Hermes(path.with_suffix(".toml"))
            call_hermes_cmd(
                hermes,
                connection_only,
                incentivized,
                version,
            )
        else:
            w3 = cronos.w3
            acc = derive_new_account(2)
            sender = acc.address
            # fund new sender to deploy contract with same address
            if w3.eth.get_balance(sender, "latest") == 0:
                fund = 3000000000000000000
                tx = {"to": sender, "value": fund, "gasPrice": w3.eth.gas_price}
                send_transaction(w3, tx)
                assert w3.eth.get_balance(sender, "latest") == fund
            caller = deploy_contract(w3, CONTRACTS["TestRelayer"], key=acc.key).address
            assert caller == RELAYER_CALLER, caller
            call_rly_cmd(path, connection_only, version)

        if incentivized:
            # register fee payee
            src_chain = cronos.cosmos_cli()
            dst_chain = chainmain.cosmos_cli()
            rsp = dst_chain.register_counterparty_payee(
                "transfer",
                "channel-0",
                dst_chain.address("relayer"),
                src_chain.address("signer1"),
                from_="relayer",
                fees="100000000basecro",
            )
            assert rsp["code"] == 0, rsp["raw_log"]

        port = None
        if is_relay:
            cronos.supervisorctl("start", "relayer-demo")
            if is_hermes:
                port = hermes.port
            else:
                port = 5183
        yield IBCNetwork(cronos, chainmain, hermes, incentivized)
        if port:
            wait_for_port(port)


def assert_ready(ibc):
    # wait for hermes
    output = subprocess.getoutput(
        f"curl -s -X GET 'http://127.0.0.1:{ibc.hermes.port}/state' | jq"
    )
    assert json.loads(output)["status"] == "success"


def hermes_transfer(ibc):
    assert_ready(ibc)
    # chainmain-1 -> cronos_777-1
    my_ibc0 = "chainmain-1"
    my_ibc1 = "cronos_777-1"
    my_channel = "channel-0"
    dst_addr = eth_to_bech32(ADDRS["signer2"])
    src_amount = 10
    src_denom = "basecro"
    # dstchainid srcchainid srcportid srchannelid
    cmd = (
        f"hermes --config {ibc.hermes.configpath} tx ft-transfer "
        f"--dst-chain {my_ibc1} --src-chain {my_ibc0} --src-port transfer "
        f"--src-channel {my_channel} --amount {src_amount} "
        f"--timeout-height-offset 1000 --number-msgs 1 "
        f"--denom {src_denom} --receiver {dst_addr} --key-name relayer"
    )
    subprocess.run(cmd, check=True, shell=True)
    return src_amount


def rly_transfer(ibc):
    # chainmain-1 -> cronos_777-1
    my_ibc0 = "chainmain-1"
    my_ibc1 = "cronos_777-1"
    channel = "channel-0"
    dst_addr = eth_to_bech32(ADDRS["signer2"])
    src_amount = 10
    src_denom = "basecro"
    path = ibc.cronos.base_dir.parent / "relayer"
    # srcchainid dstchainid amount dst_addr srchannelid
    cmd = (
        f"rly tx transfer {my_ibc0} {my_ibc1} {src_amount}{src_denom} "
        f"{dst_addr} {channel} "
        f"--path chainmain-cronos "
        f"--home {str(path)}"
    )
    subprocess.run(cmd, check=True, shell=True)


def assert_duplicate(base_port, height):
    port = ports.rpc_port(base_port)
    url = f"http://127.0.0.1:{port}/block_results?height={height}"
    res = requests.get(url).json().get("result")
    events = res["txs_results"][0]["events"]
    values = set()
    for event in events:
        if event["type"] == "message":
            continue
        str = json.dumps(event)
        assert str not in values, f"dup event find: {str}"
        values.add(str)


def find_duplicate(attributes):
    res = set()
    key = attributes[0]["key"]
    for attribute in attributes:
        if attribute["key"] == key:
            value0 = attribute["value"]
        elif attribute["key"] == "amount":
            amount = attribute["value"]
            value_pair = f"{value0}:{amount}"
            if value_pair in res:
                return value_pair
            res.add(value_pair)
    return None


def ibc_transfer_with_hermes(ibc):
    src_amount = hermes_transfer(ibc)
    dst_amount = src_amount * RATIO  # the decimal places difference
    dst_denom = "basetcro"
    dst_addr = eth_to_bech32(ADDRS["signer2"])
    old_dst_balance = get_balance(ibc.cronos, dst_addr, dst_denom)

    new_dst_balance = 0

    def check_balance_change():
        nonlocal new_dst_balance
        new_dst_balance = get_balance(ibc.cronos, dst_addr, dst_denom)
        return new_dst_balance != old_dst_balance

    wait_for_fn("balance change", check_balance_change)
    assert old_dst_balance + dst_amount == new_dst_balance
    # assert that the relayer transactions do enables the dynamic fee extension option.
    cli = ibc.cronos.cosmos_cli()
    criteria = "message.action='/ibc.core.channel.v1.MsgChannelOpenInit'"
    tx = cli.tx_search(criteria)["txs"][0]
    events = parse_events_rpc(tx["events"])
    fee = int(events["tx"]["fee"].removesuffix(dst_denom))
    gas = int(tx["gas_wanted"])
    # the effective fee is decided by the max_priority_fee (base fee is zero)
    # rather than the normal gas price
    assert fee == gas * 1000000

    # check duplicate OnRecvPacket events
    criteria = "message.action='/ibc.core.channel.v1.MsgRecvPacket'"
    tx = cli.tx_search(criteria)["txs"][0]
    events = tx["events"]
    for event in events:
        dup = find_duplicate(event["attributes"])
        assert not dup, f"duplicate {dup} in {event['type']}"


def get_balance(chain, addr, denom):
    balance = chain.cosmos_cli().balance(addr, denom)
    print("balance", balance, addr, denom)
    return balance


def get_balances(chain, addr):
    return chain.cosmos_cli().balances(addr)


def ibc_multi_transfer(ibc):
    chains = [ibc.cronos.cosmos_cli(), ibc.chainmain.cosmos_cli()]
    # FIXME: more users after batch fix
    users = [f"user{i}" for i in range(1, 2)]
    addrs0 = [chains[0].address(user) for user in users]
    addrs1 = [chains[1].address(user) for user in users]
    denom0 = "basetcro"
    denom1 = "basecro"
    channel0 = "channel-0"
    channel1 = "channel-0"
    old_balance0 = 30000000000000000000000
    old_balance1 = 1000000000000000000000
    path = f"transfer/{channel1}/{denom0}"
    denom_hash = hashlib.sha256(path.encode()).hexdigest().upper()
    amount = 1000
    expected = [
        {"denom": denom1, "amount": f"{old_balance1}"},
        {"denom": f"ibc/{denom_hash}", "amount": f"{amount}"},
    ]

    for i, _ in enumerate(users):
        rsp = chains[0].ibc_transfer(
            addrs0[i],
            addrs1[i],
            f"{amount}{denom0}",
            channel0,
            1,
            fees=f"1000{denom1}",
            event_query_tx_for=True,
        )
        assert rsp["code"] == 0, rsp["raw_log"]
        balance = chains[1].balance(addrs1[i], denom1)
        assert balance == old_balance1, balance
        balance = chains[0].balance(addrs0[i], denom0)
        assert balance == old_balance0 - amount, balance

    def assert_trace_balance(addr):
        balance = chains[1].balances(addr)
        if len(balance) > 1:
            assert balance == expected, balance
            return True
        else:
            return False

    denom_trace = chains[0].ibc_denom_trace(path, ibc.chainmain.node_rpc(0))
    assert denom_trace == {"path": f"transfer/{channel1}", "base_denom": denom0}
    for i, _ in enumerate(users):
        wait_for_fn("assert balance", lambda: assert_trace_balance(addrs1[i]))

    # chainmain-1 -> cronos_777-1
    amt = amount // 2

    def assert_balance(addr):
        balance = chains[0].balance(addr, denom0)
        if balance > old_balance0 - amount:
            assert balance == old_balance0 - amt, balance
            return True
        else:
            return False

    for _ in range(0, 2):
        for i, _ in enumerate(users):
            rsp = chains[1].ibc_transfer(
                addrs1[i],
                addrs0[i],
                f"{amt}ibc/{denom_hash}",
                channel1,
                1,
                fees=f"100000000{denom1}",
            )
            assert rsp["code"] == 0, rsp["raw_log"]

        for i, _ in enumerate(users):
            wait_for_fn("assert balance", lambda: assert_balance(addrs0[i]))

        old_balance0 += amt


def ibc_incentivized_transfer(ibc):
    chains = [ibc.cronos.cosmos_cli(), ibc.chainmain.cosmos_cli()]
    receiver = chains[1].address("signer2")
    sender = chains[0].address("signer2")
    relayer = chains[0].address("signer1")
    relayer_caller = eth_to_bech32(RELAYER_CALLER)
    amount = 1000
    fee_denom = "ibcfee"
    base_denom = "basetcro"
    old_amt_fee = chains[0].balance(relayer, fee_denom)
    old_amt_fee_caller = chains[0].balance(relayer_caller, fee_denom)
    old_amt_sender_fee = chains[0].balance(sender, fee_denom)
    old_amt_sender_base = chains[0].balance(sender, base_denom)
    old_amt_receiver_base = chains[1].balance(receiver, "basecro")
    assert old_amt_sender_base == 30000000000100000000000
    assert old_amt_receiver_base == 1000000000000000000000
    src_channel = "channel-0"
    dst_channel = "channel-0"
    rsp = chains[0].ibc_transfer(
        sender,
        receiver,
        f"{amount}{base_denom}",
        src_channel,
        1,
        fees="0basecro",
    )
    assert rsp["code"] == 0, rsp["raw_log"]
    src_chain = ibc.cronos.cosmos_cli()
    rsp = src_chain.event_query_tx_for(rsp["txhash"])

    def cb(attrs):
        return "packet_sequence" in attrs

    evt = find_log_event_attrs(rsp["events"], "send_packet", cb)
    print("packet event", evt)
    packet_seq = int(evt["packet_sequence"])
    fee = f"10{fee_denom}"
    rsp = chains[0].pay_packet_fee(
        "transfer",
        src_channel,
        packet_seq,
        recv_fee=fee,
        ack_fee=fee,
        timeout_fee=fee,
        from_=sender,
    )
    assert rsp["code"] == 0, rsp["raw_log"]
    # fee is locked
    current = chains[0].balance(sender, fee_denom)
    # https://github.com/cosmos/ibc-go/pull/5571
    assert current == old_amt_sender_fee - 20, current

    # wait for relayer receive the fee
    def check_fee():
        amount = chains[0].balance(relayer, fee_denom)
        if amount > old_amt_fee:
            amount_caller = chains[0].balance(relayer_caller, fee_denom)
            if amount_caller > 0:
                assert amount_caller == old_amt_fee_caller + 10, amount_caller
                assert amount == old_amt_fee + 10, amount
            else:
                assert amount == old_amt_fee + 20, amount
            return True
        else:
            return False

    wait_for_fn("wait for relayer to receive the fee", check_fee)

    # timeout fee is refunded
    actual = get_balances(ibc.cronos, sender)
    assert actual == [
        {"denom": base_denom, "amount": f"{old_amt_sender_base - amount}"},
        {"denom": fee_denom, "amount": f"{old_amt_sender_fee - 20}"},
    ], actual
    path = f"transfer/{dst_channel}/{base_denom}"
    denom_hash = hashlib.sha256(path.encode()).hexdigest().upper()
    denom_trace = chains[0].ibc_denom_trace(path, ibc.chainmain.node_rpc(0))
    assert denom_trace == {"path": f"transfer/{dst_channel}", "base_denom": base_denom}
    current = get_balances(ibc.chainmain, receiver)
    assert current == [
        {"denom": "basecro", "amount": f"{old_amt_receiver_base}"},
        {"denom": f"ibc/{denom_hash}", "amount": f"{amount}"},
    ], current
    # transfer back
    fee_amount = 100000000
    rsp = chains[1].ibc_transfer(
        receiver,
        sender,
        f"{amount}ibc/{denom_hash}",
        dst_channel,
        1,
        fees=f"{fee_amount}basecro",
    )
    assert rsp["code"] == 0, rsp["raw_log"]

    def check_balance_change():
        return chains[0].balance(sender, base_denom) != old_amt_sender_base - amount

    wait_for_fn("balance change", check_balance_change)
    actual = chains[0].balance(sender, base_denom)
    assert actual == old_amt_sender_base, actual
    current = chains[1].balance(receiver, "basecro")
    assert current == old_amt_receiver_base - fee_amount
    return amount, packet_seq


def ibc_denom(channel, denom):
    h = hashlib.sha256(f"transfer/{channel}/{denom}".encode()).hexdigest().upper()
    return f"ibc/{h}"


def cronos_transfer_source_tokens(ibc):
    # deploy crc21 contract
    w3 = ibc.cronos.w3
    contract, denom = setup_token_mapping(ibc.cronos, "TestERC21Source", "DOG")
    # send token to crypto.org
    print("send to crypto.org")
    chainmain_receiver = ibc.chainmain.cosmos_cli().address("signer2")
    dest_denom = ibc_denom("channel-0", denom)
    amount = 1000

    # check and record receiver balance
    chainmain_receiver_balance = get_balance(
        ibc.chainmain, chainmain_receiver, dest_denom
    )
    assert chainmain_receiver_balance == 0

    # send to ibc
    tx = contract.functions.send_to_ibc_v2(
        chainmain_receiver, amount, 0, b""
    ).build_transaction({"from": ADDRS["validator"]})
    txreceipt = send_transaction(w3, tx)
    assert txreceipt.status == 1, "should success"

    # check balance
    chainmain_receiver_new_balance = 0

    def check_chainmain_balance_change():
        nonlocal chainmain_receiver_new_balance
        chainmain_receiver_new_balance = get_balance(
            ibc.chainmain, chainmain_receiver, dest_denom
        )
        chainmain_receiver_all_balance = get_balances(ibc.chainmain, chainmain_receiver)
        print("receiver all balance:", chainmain_receiver_all_balance)
        return chainmain_receiver_balance != chainmain_receiver_new_balance

    wait_for_fn("check balance change", check_chainmain_balance_change)
    assert chainmain_receiver_new_balance == amount

    # check legacy send to ibc
    tx = contract.functions.send_to_ibc(chainmain_receiver, 1).build_transaction(
        {"from": ADDRS["validator"]}
    )
    txreceipt = send_transaction(w3, tx)
    assert txreceipt.status == 0, "should fail"

    # send back the token to cronos
    # check receiver balance
    cronos_balance_before_send = contract.caller.balanceOf(ADDRS["signer2"])
    assert cronos_balance_before_send == 0

    # send back token through ibc
    print("Send back token through ibc")
    chainmain_cli = ibc.chainmain.cosmos_cli()
    cronos_receiver = eth_to_bech32(ADDRS["signer2"])

    coin = "1000" + dest_denom
    fees = "100000000basecro"
    rsp = chainmain_cli.ibc_transfer(
        chainmain_receiver, cronos_receiver, coin, "channel-0", 1, fees=fees
    )
    assert rsp["code"] == 0, rsp["raw_log"]

    # check contract balance
    cronos_balance_after_send = 0

    def check_contract_balance_change():
        nonlocal cronos_balance_after_send
        cronos_balance_after_send = contract.caller.balanceOf(ADDRS["signer2"])
        return cronos_balance_after_send != cronos_balance_before_send

    wait_for_fn("check contract balance change", check_contract_balance_change)
    assert cronos_balance_after_send == amount
    return amount, contract.address


def cronos_transfer_source_tokens_with_proxy(ibc):
    w3 = ibc.cronos.w3
    symbol = "TEST"
    contract, denom = setup_token_mapping(ibc.cronos, "TestCRC20", symbol)

    # deploy crc20 proxy contract
    proxycrc20 = deploy_contract(
        w3,
        CONTRACTS["TestCRC20Proxy"],
        (contract.address, True),
    )

    print("proxycrc20 contract deployed at address: ", proxycrc20.address)
    assert proxycrc20.caller.is_source()
    assert proxycrc20.caller.crc20() == contract.address

    cronos_cli = ibc.cronos.cosmos_cli()
    # change token mapping
    rsp = cronos_cli.update_token_mapping(
        denom, proxycrc20.address, symbol, 6, from_="validator"
    )
    assert rsp["code"] == 0, rsp["raw_log"]
    wait_for_new_blocks(cronos_cli, 1)

    print("check the contract mapping exists now")
    rsp = cronos_cli.query_denom_by_contract(proxycrc20.address)
    assert rsp["denom"] == denom

    # send token to crypto.org
    print("send to crypto.org")
    chainmain_receiver = ibc.chainmain.cosmos_cli().address("signer2")
    dest_denom = ibc_denom("channel-0", denom)
    amount = 1000
    sender = ADDRS["validator"]

    # First we need to approve the proxy contract to move asset
    tx = contract.functions.approve(proxycrc20.address, amount).build_transaction(
        {"from": sender}
    )
    txreceipt = send_transaction(w3, tx)
    assert txreceipt.status == 1, "should success"
    assert contract.caller.allowance(ADDRS["validator"], proxycrc20.address) == amount

    # check and record receiver balance
    chainmain_receiver_balance = get_balance(
        ibc.chainmain, chainmain_receiver, dest_denom
    )
    assert chainmain_receiver_balance == 0

    # send to ibc
    tx = proxycrc20.functions.send_to_ibc(
        chainmain_receiver, amount, 0, b""
    ).build_transaction({"from": sender})
    txreceipt = send_transaction(w3, tx)
    print(txreceipt)
    assert txreceipt.status == 1, "should success"

    # check balance
    chainmain_receiver_new_balance = 0

    def check_chainmain_balance_change():
        nonlocal chainmain_receiver_new_balance
        chainmain_receiver_new_balance = get_balance(
            ibc.chainmain, chainmain_receiver, dest_denom
        )
        chainmain_receiver_all_balance = get_balances(ibc.chainmain, chainmain_receiver)
        print("receiver all balance:", chainmain_receiver_all_balance)
        return chainmain_receiver_balance != chainmain_receiver_new_balance

    wait_for_fn("check balance change", check_chainmain_balance_change)
    assert chainmain_receiver_new_balance == amount

    # send back the token to cronos
    # check receiver balance
    cronos_balance_before_send = contract.caller.balanceOf(ADDRS["signer2"])
    assert cronos_balance_before_send == 0

    # send back token through ibc
    print("Send back token through ibc")
    chainmain_cli = ibc.chainmain.cosmos_cli()
    cronos_receiver = eth_to_bech32(ADDRS["signer2"])

    coin = f"{amount}{dest_denom}"
    fees = "100000000basecro"
    rsp = chainmain_cli.ibc_transfer(
        chainmain_receiver, cronos_receiver, coin, "channel-0", 1, fees=fees
    )
    assert rsp["code"] == 0, rsp["raw_log"]

    # check contract balance
    cronos_balance_after_send = 0

    def check_contract_balance_change():
        nonlocal cronos_balance_after_send
        cronos_balance_after_send = contract.caller.balanceOf(ADDRS["signer2"])
        return cronos_balance_after_send != cronos_balance_before_send

    wait_for_fn("check contract balance change", check_contract_balance_change)
    assert cronos_balance_after_send == amount
    return amount, contract.address


def wait_for_check_channel_ready(cli, connid, channel_id, target="STATE_OPEN"):
    print("wait for channel ready", channel_id, target)

    def check_channel_ready():
        channels = cli.ibc_query_channels(connid)["channels"]
        try:
            state = next(
                channel["state"]
                for channel in channels
                if channel["channel_id"] == channel_id
            )
        except StopIteration:
            return False
        return state == target

    wait_for_fn("channel ready", check_channel_ready, timeout=30)


def get_next_channel(cli, connid):
    prefix = "channel-"
    channels = cli.ibc_query_channels(connid)["channels"]
    c = 0
    if len(channels) > 0:
        c = max(channel["channel_id"] for channel in channels)
        c = int(c.removeprefix(prefix)) + 1
    return f"{prefix}{c}"


def wait_for_check_tx(cli, adr, num_txs, timeout=None):
    print("wait for tx arrive")

    def check_tx():
        current = len(cli.query_all_txs(adr)["txs"])
        print("current", current)
        return current > num_txs

    if timeout is None:
        wait_for_fn("transfer tx", check_tx)
    else:
        try:
            print(f"should assert timeout err when pass {timeout}s")
            wait_for_fn("transfer tx", check_tx, timeout=timeout)
        except TimeoutError:
            raised = True
        assert raised


def wait_for_status_change(tcontract, channel_id, seq, timeout=None):
    print(f"wait for status change for {seq}")

    def check_status():
        status = tcontract.caller.getStatus(channel_id, seq)
        return status

    if timeout is None:
        wait_for_fn("current status", check_status)
    else:
        try:
            print(f"should assert timeout err when pass {timeout}s")
            wait_for_fn("current status", check_status, timeout=timeout)
        except TimeoutError:
            raised = True
        assert raised


def register_acc(cli, connid):
    print("register ica account")
    v = json.dumps({"fee_version": "ics29-1", "app_version": ""})
    rsp = cli.icaauth_register_account(connid, from_="signer2", gas="400000", version=v)
    _, channel_id = assert_channel_open_init(rsp)
    wait_for_check_channel_ready(cli, connid, channel_id)

    print("query ica account")
    ica_address = cli.ica_query_account(
        connid,
        cli.address("signer2"),
    )["interchain_account_address"]
    print("ica address", ica_address, "channel_id", channel_id)
    return ica_address, channel_id


def funds_ica(cli, adr):
    # initial balance of interchain account should be zero
    assert cli.balance(adr) == 0

    # send some funds to interchain account
    rsp = cli.transfer("signer2", adr, "1cro", gas_prices="1000000basecro")
    assert rsp["code"] == 0, rsp["raw_log"]
    wait_for_new_blocks(cli, 1)
    amt = 100000000
    # check if the funds are received in interchain account
    assert cli.balance(adr, denom="basecro") == amt
    return amt


def assert_channel_open_init(rsp):
    assert rsp["code"] == 0, rsp["raw_log"]
    port_id, channel_id = next(
        (
            evt["attributes"][0]["value"],
            evt["attributes"][1]["value"],
        )
        for evt in rsp["events"]
        if evt["type"] == "channel_open_init"
    )
    print("port-id", port_id, "channel-id", channel_id)
    return port_id, channel_id


def gen_send_msg(sender, receiver, denom, amount):
    return {
        "@type": "/cosmos.bank.v1beta1.MsgSend",
        "from_address": sender,
        "to_address": receiver,
        "amount": [{"denom": denom, "amount": f"{amount}"}],
    }


def ica_ctrl_send_tx(
    cli_host,
    cli_controller,
    connid,
    ica_address,
    msg_num,
    receiver,
    denom,
    amount,
    memo=None,
    incentivized_cb=None,
    **kwargs,
):
    num_txs = len(cli_host.query_all_txs(ica_address)["txs"])
    # generate a transaction to send to host chain
    m = gen_send_msg(ica_address, receiver, denom, amount)
    msgs = []
    for i in range(msg_num):
        msgs.append(m)
    data = json.dumps(msgs)
    packet = cli_controller.ica_generate_packet_data(data, json.dumps(memo))
    # submit transaction on host chain on behalf of interchain account
    rsp = cli_controller.ica_ctrl_send_tx(
        connid,
        json.dumps(packet),
        from_="signer2",
        **kwargs,
    )
    assert rsp["code"] == 0, rsp["raw_log"]
    events = parse_events_rpc(rsp["events"])
    seq = int(events.get("send_packet")["packet_sequence"])
    if incentivized_cb:
        incentivized_cb(seq)
    wait_for_check_tx(cli_host, ica_address, num_txs)
    return seq


def log_gas_records(cli):
    criteria = "tx.height >= 0"
    txs = cli.tx_search_rpc(criteria)
    records = []
    for tx in txs:
        res = tx["tx_result"]
        if res["gas_used"]:
            records.append(res["gas_used"])
    return records
