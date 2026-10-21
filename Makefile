.PHONY: help dry-run dry-run-v14 test check

help:
	@printf 'make dry-run     # modular entry, no network\nmake dry-run-v14 # original V14, no network\nmake test        # offline tests\nmake check       # Python compile checks\n'

dry-run:
	python3 miplay_client.py

dry-run-v14:
	python3 miplay_client_v14.py

test:
	python3 -m unittest discover -s tests -v

check:
	python3 -m compileall -q src tools tests miplay_client.py miplay_client_v14.py
