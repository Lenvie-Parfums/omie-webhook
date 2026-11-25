def post_worker_init(worker):
    # Starts in each child process (safe with --preload); portal claims are atomic.
    from app import start_portal_queue
    start_portal_queue()
