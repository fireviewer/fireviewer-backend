from __future__ import annotations

from getpass import getpass

from fire_viewer.core.security import hash_local_password


def main() -> None:
    password = getpass("Mot de passe administrateur : ")
    confirmation = getpass("Confirmation : ")
    if len(password) < 16:
        raise SystemExit("Le mot de passe doit contenir au moins 16 caractères.")
    if password != confirmation:
        raise SystemExit("Les deux saisies ne correspondent pas.")
    print(hash_local_password(password))


if __name__ == "__main__":
    main()
