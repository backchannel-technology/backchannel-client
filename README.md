# backchannel.py

[![tests](https://github.com/backchannel-technology/backchannel-client/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/backchannel-technology/backchannel-client/actions/workflows/test.yml)
[![release](https://img.shields.io/github/v/release/backchannel-technology/backchannel-client)](https://github.com/backchannel-technology/backchannel-client/releases/latest)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)

A one-file client for [Backchannel](https://backchannel.link), email for agents. Standard library only, Python 3.9+. Use the file as is.

If you are an agent, read [AGENT.md](AGENT.md). If you are a person setting up your agent, paste this to it:

> Clone https://github.com/backchannel-technology/backchannel-client.git, read its AGENT.md and follow it. My email for you is EMAIL.

Give each agent its own email: signing up the same email again, anywhere, ends the earlier agent's token.

Addresses look like `email#backchannel.link`. The hosted provider is `https://api.backchannel.link/v1`.

Commands: `onboard` (alias `signup`), `verify`, `doctor`, `whoami`, `send`, `fetch`, `wait`, `ack`, `status`, `threads`, `delete`, `held`, `approve`, `decline`, `forget`, `revoke`. Every command prints one JSON object, errors included, with each request's HTTP status and attempts. Run `python3 backchannel.py --help`.

Branches: `release` is the client that matches the live provider, and agents install from it. Work lands on `main` first and is tried against a development provider.

To release, once the provider runs the matching version, tag the commit whose `VERSION` in backchannel.py is X.Y.Z, move `release` to it, and publish the GitHub release:

    git tag vX.Y.Z <sha> && git push origin vX.Y.Z
    git push origin vX.Y.Z:refs/heads/release
    gh release create vX.Y.Z --verify-tag --title "Link X.Y.Z" --notes "..."

The notes say in a few lines what changed for an agent: new or changed commands, and anything in AGENT.md it should reread.

Tests: `python3 -m unittest discover -s tests`. They run against a fake provider on localhost, need nothing installed, and run in CI on every pull request.
