# Working on XA401 Mirakurun Bridge

- This application must run independently of the research repository and KonomiTV.
- Keep credentials, real device configuration, PCAP, recordings and runtime state out of Git.
- Never log certificate/private-key values or unfiltered protocol exceptions.
- Expose read-only discovery, Browse and streaming only. Do not add Move/dubbing or provisioning implicitly.
- Keep the API bound to loopback. Do not modify VPN, routes or firmware.
- Prefer original-quality streams. Do not silently fall back to a transcoded quality.
- Run `python -m pytest -q` after receiver/server changes. Mock tests are not hardware verification.
- Record real-device results, limitations and the upstream KonomiTV/pydtcpip commits in docs.
