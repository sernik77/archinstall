import re
import sys
import time
from collections.abc import Callable
from pathlib import Path

from archinstall.lib.command import SysCommand
from archinstall.lib.exceptions import RequirementError, SysCallError
from archinstall.lib.log import debug, error, info, warn
from archinstall.lib.pathnames import PACMAN_CONF
from archinstall.lib.plugins import plugins
from archinstall.lib.translationhandler import tr


class Pacman:
	def __init__(self, target: Path, silent: bool = False):
		self.synced = False
		self.silent = silent
		self.target = target

	@staticmethod
	def run(args: str, default_cmd: str = 'pacman') -> SysCommand:
		"""
		A centralized function to call `pacman` from.
		It also protects us from colliding with other running pacman sessions (if used locally).
		The grace period is set to 10 minutes before exiting hard if another pacman instance is running.
		"""
		pacman_db_lock = Path('/var/lib/pacman/db.lck')

		if pacman_db_lock.exists():
			warn(tr('Pacman is already running, waiting maximum 10 minutes for it to terminate.'))

		started = time.monotonic()
		while pacman_db_lock.exists():
			time.sleep(0.25)

			if time.monotonic() - started > (60 * 10):
				error(tr('Pre-existing pacman lock never exited. Please clean up any existing pacman sessions before using archinstall.'))
				sys.exit(1)

		return SysCommand(f'{default_cmd} {args}')

	def ask(self, error_message: str, bail_message: str, func: Callable, *args, **kwargs) -> None:  # type: ignore[no-untyped-def, type-arg]
		while True:
			try:
				func(*args, **kwargs)
				break
			except Exception as err:
				error(f'{error_message}: {err}')
				if not self.silent and input('Would you like to re-try this download? (Y/n): ').lower().strip() in 'y':
					continue
				raise RequirementError(f'{bail_message}: {err}')

	def sync(self) -> None:
		if self.synced:
			return

		try:
			self.run('-Syy')
		except SysCallError as err:
			if b'GPGME' in err.worker_log or b'keyring' in err.worker_log.lower():
				warn('Pacman sync failed with keyring error, attempting keyring reinit')
				self._reinit_keyring()
				msg = 'Could not sync a new package database after keyring reinit'
			else:
				msg = 'Could not sync a new package database'

			self.ask(msg, 'Could not sync mirrors', self.run, '-Syy')

		self.synced = True

	@staticmethod
	def _is_running(process: str) -> bool:
		try:
			SysCommand(f'pgrep -x {process}')
			return True
		except SysCallError:
			debug(f'{process} is not running')
			return False

	@staticmethod
	def _reinit_keyring() -> None:
		if Pacman._is_running('gpg-agent'):
			try:
				SysCommand('killall gpg-agent')
			except SysCallError as err:
				debug(f'Failed to kill gpg-agent: {err}')

		try:
			SysCommand('pacman-key --init')
			SysCommand('pacman-key --populate archlinux')
			debug('Keyring reinitialized successfully')
		except SysCallError as err:
			debug(f'Keyring reinit failed: {err}')

	def strap(self, packages: str | list[str]) -> None:
		self.sync()
		if isinstance(packages, str):
			packages = [packages]

		for plugin in plugins.values():
			if hasattr(plugin, 'on_pacstrap'):
				if result := plugin.on_pacstrap(packages):
					packages = result

		packages = list(dict.fromkeys(packages))  # preserve order, remove dups

		while True:
			info(f'Installing packages: {packages}')

			try:
				SysCommand(
					f'pacstrap -C {PACMAN_CONF} -K {self.target} {" ".join(packages)} --noconfirm --needed',
					peek_output=True,
				)
				return
			except SysCallError as err:
				action = self._handle_pacstrap_conflict(err, packages)

				if action == 'retry':
					continue
				elif action in ('skip', 'force'):
					return

				action = self._handle_pacstrap_missing(err, packages)

				if action == 'retry':
					continue
				elif action in ('skip', 'force'):
					return

				# fallback to legacy yes/no retry for other errors
				error('Could not strap in packages')
				if not self.silent and input('Would you like to re-try this download? (Y/n): ').lower().strip() in 'y':
					continue

				raise RequirementError(
					'Pacstrap failed. See /var/log/archinstall/install.log or above message for error details',
				) from err

	def _handle_pacstrap_conflict(self, err: SysCallError, packages: list[str]) -> str | None:
		"""
		Handle package conflicts interactively.
		Returns:
			'retry' to attempt pacstrap again,
			'skip' to skip this package set,
			'force' to ignore the error and continue,
			None to fall back to legacy handling.
		"""
		conflict = self._parse_conflict(str(err))

		if conflict is None:
			return None

		pkg_a, pkg_b, remove_candidate = conflict

		if self.silent:
			return None

		choices = [
			('remove', f'Remove {remove_candidate} and retry'),
			('choose_a', f'Choose {pkg_a}'),
			('choose_b', f'Choose {pkg_b}'),
			('force', 'Force and continue'),
			('skip', 'Skip this step and continue'),
		]

		print('\nPackage conflict detected:')
		print(f'  {pkg_a} <-> {pkg_b}')

		for idx, (_, label) in enumerate(choices, 1):
			print(f'  {idx}) {label}')

		selection = None
		while selection is None:
			try:
				val = input('Select an option (1-5): ').strip()
				idx = int(val) if val else 0
				if 1 <= idx <= len(choices):
					selection = choices[idx - 1][0]
				else:
					print('Invalid selection, try again.')
			except ValueError:
				print('Invalid selection, try again.')

		match selection:
			case 'remove':
				new_packages = [p for p in packages if self._strip_pkg_version(p) != self._strip_pkg_version(remove_candidate)]
				desc = f'Remove {remove_candidate} and retry'
				if not self._confirm_choice(selection, desc):
					return None
				packages[:] = new_packages
				info(f'Retrying without {remove_candidate}')
				return 'retry'
			case 'choose_a':
				base_a = self._strip_pkg_version(pkg_a)
				base_b = self._strip_pkg_version(pkg_b)
				new_packages = [p for p in packages if self._strip_pkg_version(p) != base_b]
				if all(self._strip_pkg_version(p) != base_a for p in new_packages):
					new_packages.append(base_a)
				desc = f'Choose {base_a} and drop {base_b}'
				if not self._confirm_choice(selection, desc):
					return None
				packages[:] = new_packages
				info(f'Retrying with {base_a}, dropping {base_b}')
				return 'retry'
			case 'choose_b':
				base_a = self._strip_pkg_version(pkg_a)
				base_b = self._strip_pkg_version(pkg_b)
				new_packages = [p for p in packages if self._strip_pkg_version(p) != base_a]
				if all(self._strip_pkg_version(p) != base_b for p in new_packages):
					new_packages.append(base_b)
				desc = f'Choose {base_b} and drop {base_a}'
				if not self._confirm_choice(selection, desc):
					return None
				packages[:] = new_packages
				info(f'Retrying with {base_b}, dropping {base_a}')
				return 'retry'
			case 'force':
				desc = 'Force past the conflict (may leave packages missing)'
				if not self._confirm_choice(selection, desc):
					return None
				warn('Forcing past pacstrap error; packages may be missing or unresolved.')
				return 'force'
			case 'skip':
				desc = 'Skip this package set and continue installation'
				if not self._confirm_choice(selection, desc):
					return None
				warn('Skipping this package set and continuing installation.')
				packages.clear()
				return 'skip'

		return None

	def _parse_conflict(self, output: str) -> tuple[str, str, str] | None:
		"""
		Extract conflicting packages from pacman output.
		Returns (pkg_a, pkg_b, remove_candidate)
		"""
		pkg_a = pkg_b = remove_candidate = None

		for line in output.splitlines():
			if 'are in conflict' in line:
				if m := re.search(r'::\s*([^\s]+)\s+and\s+([^\s]+)\s+are in conflict', line):
					pkg_a, pkg_b = m.group(1), m.group(2)
			if 'Remove ' in line and '?' in line:
				if m := re.search(r'Remove\s+([^\s?]+)\?', line):
					remove_candidate = m.group(1)

		if pkg_a and pkg_b:
			return pkg_a, pkg_b, remove_candidate or pkg_b

		return None

	def _handle_pacstrap_missing(self, err: SysCallError, packages: list[str]) -> str | None:
		"""
		Handle "target not found" errors with interactive remediation.
		Returns:
			'retry', 'skip', 'force', or None to fall back.
		"""
		missing_pkg = None
		for line in str(err).splitlines():
			if 'target not found' in line:
				if m := re.search(r'target not found:\s*([^\s]+)', line):
					missing_pkg = m.group(1)
					break

		if missing_pkg is None or self.silent:
			return None

		base_missing = self._strip_pkg_version(missing_pkg)

		options = [
			('skip', f'Skip package {missing_pkg} and continue'),
			('manual', f'Correct package name manually (current: {missing_pkg})'),
			('strip_version', f'Ignore version and retry with {base_missing}'),
			('stop', 'Stop and exit'),
		]

		print(f'\nPackage {missing_pkg} not found.')
		for idx, (_, label) in enumerate(options, 1):
			print(f'  {idx}) {label}')

		selection = None
		while selection is None:
			try:
				val = input('Select an option (1-4): ').strip()
				idx = int(val) if val else 0
				if 1 <= idx <= len(options):
					selection = options[idx - 1][0]
				else:
					print('Invalid selection, try again.')
			except ValueError:
				print('Invalid selection, try again.')

		if selection == 'skip':
			desc = f'Skip {missing_pkg} and retry without it'
			if not self._confirm_choice(selection, desc):
				return None
			packages[:] = [p for p in packages if self._strip_pkg_version(p) != base_missing]
			info(f'Skipping {missing_pkg}')
			return 'retry'

		if selection == 'manual':
			new_name = input('Enter corrected package name: ').strip()
			if not new_name:
				return None
			desc = f'Use {new_name} instead of {missing_pkg}'
			if not self._confirm_choice(selection, desc):
				return None
			new_packages = []
			replaced = False
			for p in packages:
				if self._strip_pkg_version(p) == base_missing and not replaced:
					new_packages.append(new_name)
					replaced = True
				else:
					new_packages.append(p)
			if not replaced:
				new_packages.append(new_name)
			packages[:] = new_packages
			info(f'Retrying with corrected package {new_name}')
			return 'retry'

		if selection == 'strip_version':
			desc = f'Retry with {base_missing} (version stripped)'
			if not self._confirm_choice(selection, desc):
				return None
			new_packages = []
			for p in packages:
				if self._strip_pkg_version(p) == base_missing:
					if base_missing not in new_packages:
						new_packages.append(base_missing)
				else:
					new_packages.append(p)
			packages[:] = new_packages
			info(f'Retrying with {base_missing}')
			return 'retry'

		if selection == 'stop':
			desc = 'Stop installation'
			if not self._confirm_choice(selection, desc):
				return None
			raise RequirementError(f'Package {missing_pkg} not found; installation stopped.')

		return None

	def _strip_pkg_version(self, name: str) -> str:
		base = re.split(r'[<>=]', name)[0]
		if ':' in base:
			epoch, rest = base.split(':', 1)
			if rest and rest[0].isdigit():
				base = epoch
		base = re.sub(r'-\d[\w\.:+~-]*$', '', base)
		return base

	def _confirm_choice(self, choice_key: str, actions: str) -> bool:
		resp = input(f'Chosen: {choice_key}. {actions}. Continue? (Y/n): ').strip().lower()
		return resp in ('', 'y', 'yes')
