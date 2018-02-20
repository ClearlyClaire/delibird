import argparse
import re
import datetime
import random
import json
import itertools
from getpass import getpass
from mastodon import Mastodon, StreamListener
from mastodon.Mastodon import MastodonAPIError, MastodonNotFoundError
from data import MEDIA, MSGS, REWARDS

API_BASE = 'https://social.sitedethib.com'

COMMAND_RE = re.compile(r'(va voir|vole vers|va, vole vers|rends visite à|go see|go visit|fly to)\s*(.+)', re.IGNORECASE)
FREE_RE = re.compile(r"(va (te promener|jouer))|rends-toi disponible|repose-toi|va-t'en|go idle|go away|((go|play) somewhere else)|take a break", re.IGNORECASE)
CANCEL_RE = re.compile(r'reviens|arrête|annule|stop|come back|cancel', re.IGNORECASE)
MENTION_RE = re.compile(r'([a-z0-9_]+)(@[a-z0-9\.\-]+[a-z0-9]+)?', re.IGNORECASE)
LINK_RE = re.compile(r'<a href="([^"]+)"')

STATE_OWNED, STATE_IDLE, STATE_DELIVERY = range(3)
MAX_OWNED = datetime.timedelta(hours=3)


class Error(Exception):
  """Base error class for Delibird-generated errors"""
  pass

class InternalError(Error):
  """Generic internal server error"""
  def __init__(self, acct):
    Error.__init__(self)
    self.acct = acct

class InvalidFormatError(Error):
  """Error thrown when a command does not contain a properly-formatted
  account"""
  pass

class AccountNotFoundError(Error):
  """Error thrown when the requested account cannot be resolved"""
  def __init__(self, acct):
    Error.__init__(self)
    self.acct = acct


class Delibird(StreamListener):
  """Main class for the Delibird bot."""

  def __init__(self, mastodon):
    StreamListener.__init__(self)
    self.mastodon = mastodon
    self.state = STATE_IDLE
    self.owner = None
    self.target = None
    self.last_owned = datetime.datetime.now()
    self.like_count = 0
    self.visited_users = set()
    self.reward_level = -1
    self.last_idle_toot = None
    self.last_read_notification = None
    self.own_acct_id = None
    print('Delibird started!')
    self.load()
    self.resume()


  def resume(self):
    """Process missed notifications."""
    # Process all missed notifications
    last_notification = self.last_read_notification
    if last_notification is not None:
      for notification in reversed(self.mastodon.notifications(since_id=last_notification)):
        self.on_notification(notification)


  def save(self, path='state.json'):
    """Save state to a JSON file."""
    state = {'like_count': self.like_count,
             'visited_users': list(self.visited_users),
             'reward_level': self.reward_level,
             'state': self.state,
             'last_owned': self.last_owned.isoformat()}
    if self.last_read_notification is not None:
      state['last_read_notification'] = self.last_read_notification
    if self.last_idle_toot is not None:
      state['last_idle_toot'] = self.last_idle_toot.id
    if self.owner is not None:
      state['owner'] = self.owner.id
    if self.target is not None:
      state['target'] = self.target.id
    if self.own_acct_id is not None:
      state['own_acct_id'] = self.own_acct_id
    with open(path, 'w') as file:
      json.dump(state, file)


  def load(self, path='state.json'):
    """Load state from a JSON file.
    May perform API requests to retrieve status or account information."""
    try:
      with open(path, 'r') as file:
        state = json.load(file)
      self.like_count = state['like_count']
      self.visited_users = set(state['visited_users'])
      self.reward_level = state['reward_level']
      self.state = state.get('state', STATE_IDLE)
      self.last_read_notification = state.get('last_read_notification', None)
      self.own_acct_id = state.get('own_acct_id', None)
      last_idle_toot = state.get('last_idle_toot', None)
      owner = state.get('owner', None)
      target = state.get('target', None)
      last_owned = state.get('last_owned', None)
      if last_owned:
        self.last_owned = datetime.datetime.strptime(last_owned, '%Y-%m-%dT%H:%M:%S.%f')
      self.last_idle_toot = None if last_idle_toot is None else self.mastodon.status(last_idle_toot)
      self.owner = None if owner is None else self.mastodon.account(owner)
      self.target = None if target is None else self.mastodon.account(target)
    except FileNotFoundError:
      pass


  def handle_rewards(self):
    """Check rewards conditions and post appropriate toot if conditions for a
    new reward are met."""
    level = -1
    for i, reward in enumerate(REWARDS):
      if (self.like_count >= reward['required_likes']
          and len(self.visited_users) >= reward['required_users']):
        level = i
    if level > self.reward_level:
      self.reward_level = level
      self.send_toot(REWARDS[level]['msg_id'],
                     nb_likes=self.like_count, nb_users=len(self.visited_users))
      self.save()


  def upload_media(self, name):
    """Look up a media file description and upload it."""
    desc = MEDIA[name]
    media = self.mastodon.media_post(desc['file'],
                                     description='Source: %s' % desc['source'])
    print('Uploaded %s!' % name)
    return media


  def send_toot(self, msg_id, in_reply_to_id=None, **kwargs):
    """Look up a toot's description by message id and sends it."""
    print('Sending a toot… id: %s' % msg_id)
    msg = MSGS[msg_id]
    if 'media' in msg:
      if isinstance(msg['media'], dict):
        grouped_choices = ([key] * count for key, count in msg['media'].items())
        choices = list(itertools.chain.from_iterable(grouped_choices))
        media_desc = random.choice(choices)
      else:
        media_desc = msg['media']
      media = [self.upload_media(name) for name in media_desc]
    else:
      media = None
    try:
      status = self.mastodon.status_post(msg['text'].format(**kwargs),
                                         media_ids=media,
                                         in_reply_to_id=in_reply_to_id,
                                         visibility=msg.get('privacy', ''))
    except MastodonNotFoundError:
      # Original status deleted
      status = self.mastodon.status_post(msg['text'].format(**kwargs),
                                         media_ids=media,
                                         visibility=msg.get('privacy', ''))
    self.own_acct_id = status.account.id
    self.save()
    return status


  def resolve_account(self, text_with_user, status):
    """Process command text to resolve an account from account name, mention
    or profile URL"""
    receiver_acct = None
    # First, see if we have a handle we can resolve, without the leading '@'
    match = MENTION_RE.match(text_with_user)
    if match:
      if match.group(2):
        # If it's a full handle, use it
        receiver_acct = match.group(0)
      else:
        # If it's *not* a full handle, append the user's domain
        receiver_acct = '@'.join([match.group(1)] + status.account.acct.split('@')[1:])
      try:
        matches = self.mastodon.account_search(receiver_acct)
      except MastodonAPIError:
        raise InternalError(receiver_acct)
      if not matches:
        raise AccountNotFoundError(receiver_acct)
      return matches[0]

    # Maybe it's a link to their profile, or a mention. Switch to link handling.
    match = LINK_RE.search(text_with_user)
    if match:
      url = match.group(1)
      # First check if it's one of the mentioned users
      for user in status.mentions:
        if user.url == url:
          return user
      # If not, resolve it
      try:
        matches = self.mastodon.search(url, resolve=True).accounts
      except MastodonAPIError:
        raise InternalError(url)
      if matches:
        return matches[0]

    raise InvalidFormatError


  def handle_unknown_account(self, status, acct):
    """Handle unknown accounts, potentially suggesting other accounts."""
    receiver_acct = acct.split('@')
    suggested_account = None
    if len(receiver_acct) > 1:
      username, domain = receiver_acct
      try:
        matches = self.mastodon.account_search(username, limit=40)
      except MastodonAPIError:
        pass
      else:
        for match in matches:
          if match.username != username:
            continue
          if domain in match.acct:
            suggested_account = match
            break
    if suggested_account is None:
      self.send_toot('ERROR_UNKNOWN_ACCOUNT', status,
                     sender_acct=status.account.acct, acct=acct)
    else:
      self.send_toot('ERROR_UNKNOWN_ACCOUNT2', status,
                     sender_acct=status.account.acct, acct=acct,
                     suggested_acct=suggested_account.acct)


  def handle_cmd_free(self, status, match=None):
    """Handle the command that allows the bird to go idle prematurely"""
    if not self.owner or self.owner.id != status.account.id:
      return
    if self.state != STATE_OWNED:
      return
    self.state = STATE_IDLE
    self.last_idle_toot = self.send_toot('IDLE2', in_reply_to_id=status)
    self.save()


  def handle_cmd_go_see(self, status, match):
    """Handle the “go see” command requesting the bot to visit a given user"""
    text_with_user = match.group(2)
    if self.state == STATE_DELIVERY:
      self.send_toot('ERROR_DELIVERY', status, sender_acct=status.account.acct)
      return
    if self.state == STATE_OWNED and self.owner and self.owner.id != status.account.id:
      delta = self.last_owned + MAX_OWNED - datetime.datetime.now()
      minutes = delta.seconds // 60
      self.send_toot('ERROR_OWNED', status, sender_acct=status.account.acct,
                     hours=(minutes // 60), minutes=(minutes % 60))
      return

    try:
      target = self.resolve_account(text_with_user, status)
    except AccountNotFoundError as err:
      self.handle_unknown_account(status, err.acct)
      return
    except InvalidFormatError:
      self.send_toot('ERROR_INVALID_FORMAT', status,
                     sender_acct=status.account.acct)
      return
    except InternalError as err:
      self.send_toot('ERROR_INTERNAL', status,
                     sender_acct=status.account.acct, acct=err.acct)
      return

    if target.id == self.own_acct_id:
      self.send_toot('ERROR_OWN_ACCOUNT', status,
                     sender_acct=status.account.acct)
      return

    if target.id == status.account.id:
      self.send_toot('ERROR_SAME_ACCOUNT', status,
                     sender_acct=status.account.acct)
      return

    self.state = STATE_DELIVERY
    self.owner = status.account
    self.last_owned = datetime.datetime.now()
    self.target = target

    if self.last_idle_toot is not None:
      try:
        self.mastodon.status_delete(self.last_idle_toot)
      except MastodonAPIError:
        pass
      self.last_idle_toot = None

    self.send_toot('DELIVERY_START', status, sender_acct=status.account.acct, acct=self.target.acct)


  def handle_cmd_cancel(self, status, match=None):
    """Handle the “cancel” command that cancels the last ordered delivery if the
    user issuing it is the current owner and the delivery hasn't finished yet."""
    if not self.owner or self.owner.id != status.account.id:
      return
    if self.state != STATE_DELIVERY:
      return
    self.state = STATE_OWNED
    self.send_toot('DELIVERY_CANCELLED', status, sender_acct=status.account.acct,
                   acct=self.target.acct)


  def handle_mention(self, status):
    """Handle toots mentioning Delibird, which may contain commands"""
    print('Got a mention!')
    # Do not reply if multiple people are mentionned
    if len(status.mentions) > 2:
      return
    # Process commands, in order of priority
    cmds = [(COMMAND_RE, self.handle_cmd_go_see),
            (CANCEL_RE, self.handle_cmd_cancel),
            (FREE_RE, self.handle_cmd_free)]
    for regexp, handler in cmds:
      match = regexp.search(status.content)
      if match:
        handler(status, match)
        return


  def deliver(self):
    """Deliver a message to the target, updating ownership and state in the
    process"""
    self.send_toot('DELIVERED',
                   sender_acct=self.owner.acct,
                   receiver_acct=self.target.acct,
                   nb_hours=(MAX_OWNED.seconds // 3600))
    self.owner = self.target
    self.last_owned = datetime.datetime.now()
    self.state = STATE_OWNED
    self.visited_users.add(self.owner.id)
    self.save()


  def go_idle(self):
    """Turn idle and announce it with a public toot"""
    self.state = STATE_IDLE
    self.last_idle_toot = self.send_toot('IDLE')
    self.save()


  def handle_heartbeat(self):
    if self.state == STATE_DELIVERY and self.target is not None and random.random() >= 0.94:
      self.deliver()
    elif self.state == STATE_DELIVERY:
      self.handle_rewards()
    elif self.state == STATE_OWNED and datetime.datetime.now() - self.last_owned > MAX_OWNED:
      self.go_idle()


  def on_notification(self, notification):
    self.last_read_notification = notification.id
    if notification.type == 'mention':
      self.handle_mention(notification.status)
    if notification.type == 'favourite' and notification.status.visibility == 'direct':
      self.like_count += 1
      self.save()



def register(args):
  """Register app on the server"""
  Mastodon.create_app('Delibird', api_base_url=args.api_base, to_file='secrets/clientcred.secret')


def login(args):
  """Log in as the given user, generating OAuth credentials"""
  mastodon = Mastodon(client_id='secrets/clientcred.secret', api_base_url=args.api_base)
  mastodon.log_in(args.user_mail, getpass(), to_file='secrets/usercred.secret')


def run(args):
  """Run the bot"""
  mastodon = Mastodon(client_id='secrets/clientcred.secret',
                      access_token='secrets/usercred.secret',
                      api_base_url=args.api_base)
  delibird = Delibird(mastodon)
  print('Starting streaming!')
  mastodon.stream_user(delibird)


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('command', type=str, choices=['register', 'login', 'run'])
  parser.add_argument('-a', '--api-base', type=str, default=API_BASE)
  parser.add_argument('-u', '--user-mail', type=str)
  args = parser.parse_args()

  if args.command == 'register':
    register(args)
  elif args.command == 'login':
    login(args)
  elif args.command == 'run':
    run(args)

main()
