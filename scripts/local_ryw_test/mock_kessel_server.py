#!/usr/bin/env python3
"""Mock Kessel Relations gRPC server for local testing.

Implements KesselTupleService with always-success responses.
The Kafka consumer connects here instead of the real Kessel Relations API.
"""

import logging
import sys
import uuid
from concurrent import futures

import grpc
from kessel.inventory.v1beta2 import (
    delete_tuples_response_pb2,
    create_tuples_response_pb2,
    consistency_token_pb2,
    acquire_lock_response_pb2,
    tuple_service_pb2_grpc,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [MockKessel] %(message)s")
logger = logging.getLogger(__name__)


class MockKesselTupleServicer(tuple_service_pb2_grpc.KesselTupleServiceServicer):
    """Mock implementation that logs calls and returns success."""

    def __init__(self):
        self.create_count = 0
        self.delete_count = 0
        self.lock_count = 0

    def CreateTuples(self, request, context):
        self.create_count += 1
        n = len(request.tuples)
        logger.info("CreateTuples called with %d tuple(s) (total calls: %d)", n, self.create_count)
        for t in request.tuples:
            logger.info(
                "  -> %s:%s#%s@%s:%s",
                t.resource.type.namespace,
                t.resource.type.name,
                t.relation,
                t.subject.subject.type.name if t.subject.HasField("subject") else "?",
                t.subject.subject.id if t.subject.HasField("subject") else "?",
            )
        token = f"mock-token-{uuid.uuid4().hex[:8]}"
        return create_tuples_response_pb2.CreateTuplesResponse(
            consistency_token=consistency_token_pb2.ConsistencyToken(token=token)
        )

    def DeleteTuples(self, request, context):
        self.delete_count += 1
        logger.info("DeleteTuples called (total calls: %d)", self.delete_count)
        token = f"mock-token-{uuid.uuid4().hex[:8]}"
        return delete_tuples_response_pb2.DeleteTuplesResponse(
            consistency_token=consistency_token_pb2.ConsistencyToken(token=token)
        )

    def ReadTuples(self, request, context):
        logger.info("ReadTuples called (returning empty)")
        return
        yield  # noqa: make this a generator for server-streaming

    # TODO: implement ImportBulkTuples equivalent for Inventory API

    def AcquireLock(self, request, context):
        self.lock_count += 1
        lock_token = f"mock-lock-{uuid.uuid4().hex[:8]}"
        logger.info(
            "AcquireLock called for lock_id=%s, returning token=%s (total calls: %d)",
            request.lock_id,
            lock_token,
            self.lock_count,
        )
        return acquire_lock_response_pb2.AcquireLockResponse(lock_token=lock_token)


def serve(port=50051):
    """Start the mock gRPC server."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    servicer = MockKesselTupleServicer()
    tuple_service_pb2_grpc.add_KesselTupleServiceServicer_to_server(servicer, server)
    addr = f"[::]:{port}"
    server.add_insecure_port(addr)
    server.start()
    logger.info("Mock Kessel Relations gRPC server started on port %d", port)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down mock Kessel server...")
        server.stop(grace=2)
        logger.info(
            "Stats: CreateTuples=%d, DeleteTuples=%d, AcquireLock=%d",
            servicer.create_count,
            servicer.delete_count,
            servicer.lock_count,
        )


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 50051
    serve(port)
