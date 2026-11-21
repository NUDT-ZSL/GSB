"""Programmable loopback DNS test server (deterministic, no real upstream).

Reads a response manifest (samples/responses.json) and answers UDP queries
on 127.0.0.1 according to (canonical qname, qtype):
  - preset records with per-record TTL seconds (A / CNAME chains)
  - NXDOMAIN and NODATA (SOA in authority carries the negative TTL)
  - malformed garbage replies
  - configurable per-name delay
Usage: python3 samples/dns_server.py samples/responses.json
Prints READY on stdout once bound.
"""
import json
import socket
import sys
import time

import dnslib
from dnslib import QTYPE, RCODE, RR, SOA


def canon(name):
    n = str(name).strip().lower()
    return n[:-1] if n.endswith(".") else n


def load_manifest(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_reply(query, spec, default_neg_ttl):
    reply = query.reply()
    neg_ttl = spec.get("negative_ttl_s", default_neg_ttl)
    if spec.get("rcode") == "NXDOMAIN":
        reply.header.rcode = RCODE.NXDOMAIN
        reply.add_auth(RR("test.", QTYPE.SOA, ttl=neg_ttl, rdata=SOA(
            "ns.test.", "hostmaster.test.", (2024010101, 3600, 600, 86400,
                                             neg_ttl))))
        return reply
    records = spec.get("records", [])
    if not records:
        reply.add_auth(RR("test.", QTYPE.SOA, ttl=neg_ttl, rdata=SOA(
            "ns.test.", "hostmaster.test.", (2024010101, 3600, 600, 86400,
                                             neg_ttl))))
        return reply
    for rec in records:
        name = rec.get("name") or canon(query.q.qname)
        ttl = int(rec.get("ttl_s", 60))
        if rec["type"] == "A":
            reply.add_answer(RR(name, QTYPE.A, rdata=dnslib.A(rec["address"]),
                                ttl=ttl))
        elif rec["type"] == "CNAME":
            reply.add_answer(RR(name, QTYPE.CNAME,
                                rdata=dnslib.CNAME(rec["target"]), ttl=ttl))
        else:
            raise ValueError(f"unsupported record type {rec['type']}")
    return reply


def serve(manifest_path):
    manifest = load_manifest(manifest_path)
    port = int(manifest.get("port", 15353))
    default_neg_ttl = int(manifest.get("default_negative_ttl_s", 30))
    answers = manifest.get("answers", {})
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    print("READY", flush=True)
    while True:
        data, addr = sock.recvfrom(4096)
        try:
            query = dnslib.DNSRecord.parse(data)
        except Exception:
            continue
        key = f"{canon(query.q.qname)}|{QTYPE[query.q.qtype]}"
        spec = answers.get(key)
        if spec is None:
            spec = {"rcode": "NXDOMAIN"}
        delay_ms = int(spec.get("delay_ms", 0))
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        if spec.get("malformed"):
            sock.sendto(b"\x00\x01not-a-dns-message", addr)
            continue
        reply = build_reply(query, spec, default_neg_ttl)
        sock.sendto(reply.pack(), addr)


if __name__ == "__main__":
    serve(sys.argv[1] if len(sys.argv) > 1 else "samples/responses.json")
