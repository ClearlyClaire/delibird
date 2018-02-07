import argparse
import re
import time
import datetime
import random
import json
from getpass import getpass
from mastodon import Mastodon, StreamListener
from data import MEDIA, MSGS, REWARDS

API_BASE = 'https://social.sitedethib.com'

MENTION_RE = re.compile(r'[A-Za-z0-9]+@[a-z0-9\.\-]+[A-Za-z0-9]+')

STATE_OWNED, STATE_IDLE, STATE_DELIVERY = range(3)
MAX_OWNED = datetime.timedelta(hours=8)

class Delibird(StreamListener):
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
    print('Delibird started!')
    self.load()

  def save(self):
    state = {'like_count': self.like_count,
             'visited_users': list(self.visited_users),
             'reward_level': -1}
    with open('state.json', 'w') as file:
      json.dump(state, file)

  def load(self):
    try:
      with open('state.json', 'r') as file:
        state = json.load(file)
      self.like_count = state['like_count']
      self.visited_users = set(state['visited_users'])
      self.reward_level = state['reward_level']
    except FileNotFoundError:
      pass

  def handle_rewards(self):
    level = -1
    for i, reward in enumerate(REWARDS):
      if self.like_count >= reward['required_likes'] and len(self.visited_users) >= reward['required_users']:
        level = i
    if level > self.reward_level:
      self.reward_level = level
      self.send_toot(REWARDS[level]['msg_id'], nb_likes=self.like_count, nb_users=len(self.visited_users))
      self.save()

  def upload_media(self, name):
    media = self.mastodon.media_post(MEDIA[name]['file'], description='Source: %s' % MEDIA[name]['source'])
    print('Uploaded %s!' % name)
    return media

  def send_toot(self, msg_id, in_reply_to_id=None, **kwargs):
    print('Sending a toot… id: %s' % msg_id)
    msg = MSGS[msg_id]
    media = [self.upload_media(name) for name in msg['media']] if 'media' in msg else None
    self.mastodon.status_post(msg['text'].format(**kwargs), media_ids=media, in_reply_to_id=in_reply_to_id, visibility=msg.get('privacy', ''))

  def handle_mention(self, status):
    print('Got a mention!')
    if self.state == STATE_DELIVERY:
      return self.send_toot('ERROR_DELIVERY', status, sender_acct=status.account.acct)
    if self.state == STATE_OWNED and self.owner and self.owner.id != status.account.id:
      return self.send_toot('ERROR_OWNED', status, sender_acct=status.account.acct)
    match = MENTION_RE.search(status.content)
    if not match:
      return self.send_toot('ERROR_INVALID_FORMAT', status, sender_acct=status.account.acct)
    matches = self.mastodon.account_search(match.group(0))
    if not matches:
      return self.send_toot('ERROR_UNKNOWN_ACCOUNT', status, sender_acct=status.account.acct, acct=match.group(0))
    self.state = STATE_DELIVERY
    self.owner = status.account
    self.last_owned = datetime.datetime.now()
    self.target = matches[0]
    self.send_toot('DELIVERY_START', status, sender_acct=status.account.acct, acct=self.target.acct)

  def deliver(self):
    self.send_toot('DELIVERED', sender_acct=self.owner.acct, receiver_acct=self.target.acct)
    self.owner = self.target
    self.last_owned = datetime.datetime.now()
    self.state = STATE_OWNED
    self.visited_users.add(self.owner.id)
    self.save()

  def go_idle(self):
    self.state = STATE_IDLE
    self.send_toot('IDLE')

  def handle_heartbeat(self):
    if self.state == STATE_DELIVERY and self.target is not None and random.random() > 0.85:
      self.deliver()
    elif self.state == STATE_DELIVERY:
      self.handle_rewards()
    elif self.state == STATE_OWNED and datetime.datetime.now() - self.last_owned > MAX_OWNED:
      self.go_idle()

  def on_notification(self, notification):
    if notification.type == 'mention':
      self.handle_mention(notification.status)
    if notification.type == 'favourite' and notification.status.visibility == 'direct':
      self.like_count += 1
      self.save()


def register(args):
  Mastodon.create_app('Delibird', api_base_url=args.api_base, to_file='secrets/clientcred.secret')

def login(args):
  mastodon = Mastodon(client_id = 'secrets/clientcred.secret', api_base_url=args.api_base)
  mastodon.log_in(args.user_mail, getpass(), to_file='secrets/usercred.secret')

def run(args):
  mastodon = Mastodon(client_id = 'secrets/clientcred.secret',
                      access_token = 'secrets/usercred.secret',
                      api_base_url=args.api_base)
  delibird = Delibird(mastodon)
  print('Starting streaming!')
  mastodon.stream_user(delibird)

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
