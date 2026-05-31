import getpass
import sys

import keyring

from email_mcp.accounts import KEYRING_SERVICE


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m email_mcp.setup_cli <account-name>")
        sys.exit(2)
    name = sys.argv[1]
    pw = getpass.getpass(f"App-specific password for '{name}': ")
    if not pw:
        print("Empty password, aborting.")
        sys.exit(1)
    keyring.set_password(KEYRING_SERVICE, name, pw)
    print(f"Stored password for '{name}' in Keychain (service '{KEYRING_SERVICE}').")


if __name__ == "__main__":
    main()
