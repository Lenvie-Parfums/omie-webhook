 # Keep the arbiter heartbeat independent from slow Omie HTTP requests.
 # A sync worker killed after 120s also kills the portal queue thread.
worker_class = "gthread"
threads = 4
workers = 1


def post_worker_init(worker):
    # Starts in each child process (safe with --preload); portal claims are atomic.
    from app import start_portal_queue
    start_portal_queue()
