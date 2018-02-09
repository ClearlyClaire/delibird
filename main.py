import argparse
import re
import time
import datetime
import random
from getpass import getpass
from mastodon import Mastodon, StreamListener

API_BASE = 'https://social.sitedethib.com'

MENTION_RE = re.compile(r'[A-Za-z0-9]+@[a-z0-9\.\-]+[A-Za-z0-9]+')

STATE_OWNED, STATE_IDLE, STATE_DELIVERY = range(3)
MAX_OWNED = datetime.timedelta(hours=8)

MEDIAS = {
  'FLYING_AWAY': {
    'file': 'bye.mp4',
    'source': 'https://www.youtube.com/watch?v=6Qp4wafJ8_I',
  },
  'FLYING_IN': {
    'file': 'hello.mp4',
    'source': 'https://www.youtube.com/watch?v=Gu8EudiPLZw',
  },
}

MSGS = {
  'ERROR_OWNED': {
    'text': '''@{sender_acct} Pwi pwi pwi!!!

[FR] (Je suis chez quelqu'un d'autre ! Je ne peux pas être envoyé comme ça !)

[EN] (I'm currently visiting someone else! You can't send me around!)''',
    'privacy': 'direct',
   },
  'ERROR_DELIVERY': {
    'text': '''@{sender_acct} Pwiii pwii pwii pwiii!

[FR] (Je suis en train de m'envoler pour voir quelqu'un d'autre !)

[EN] (I'm flying away to someone else!)''',
    'privacy': 'direct',
  },
  'ERROR_INVALID_FORMAT': {
    'text': '''@{sender_acct} Pwii? Pwiii!

[FR] (Je n'ai pas compris qui vers qui je dois m'envoler ? Il me faut son adresse complète sans le @ initial !)

[EN] (I don't understand who I'm supposed to be flying to? Type their full handle without the leading @ sign!)''',
    'privacy': 'direct',
  },
  'ERROR_UNKNOWN_ACCOUNT': {
    'text': '''@{sender_acct} Pwiii! Pwiii?

[FR] (Je n'ai pas trouvé {acct} ! Vers qui d'autre dois-je aller ?)

[EN] (I can't find {acct}! Who else should I visit?)''',
    'privacy': 'direct',
  },
  'DELIVERY_START': {
    'text': '''@{sender_acct} Pwiipwii pwiipwiii!

[FR] (En route vers chez {acct} ! Ça peut me prendre un peu de temps !)

[EN] (On my way to {acct}! This may take me a while!)''',
    'privacy': 'direct',
    'media': ['FLYING_AWAY'],
  },
  'DELIVERED': {
    'text': '''@{receiver_acct} :caique: Pwiii pwii pwiii! :caique:

[FR] (Bonjour de la part de {sender_acct} ! Tu peux m'envoyer vers la personne de ton choix en me donnant son adresse complète sans le @ initial !)

[EN] (Hello from {sender_acct}! You can send me to anyone you'd like by telling me their full handle without the leading @ sign!)''',
    'privacy': 'direct',
    'media': ['FLYING_IN'],
  },
  'IDLE': {
    'text': ''':caique: Pwiii… pwii pwii! :caique:

[FR] (Personne ne m'a envoyé me promener depuis un moment… du coup, n'importe qui peut le faire en me donnant l'adresse complète de quelqu'un, sans le @ initial !)

[EN] (Noone sent me flying away… but now, anybody can! Give me someone's full handle without the leading @ sign, and I'll fly to them!)''',
    'privacy': 'public',
  }
}

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
    print('Delibird started!')

  def upload_media(self, name):
    media = self.mastodon.media_post(MEDIAS[name]['file'], description='Source: %s' % MEDIAS[name]['source'])
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

  def go_idle(self):
    self.state = STATE_IDLE
    self.send_toot('IDLE')

  def handle_heartbeat(self):
    if self.state == STATE_DELIVERY and self.target is not None and random.random() > 0.85:
      self.deliver()
    elif self.state == STATE_OWNED and datetime.datetime.now() - self.last_owned > MAX_OWNED:
      self.go_idle()

  def on_notification(self, notification):
    if notification.type == 'mention':
      self.handle_mention(notification.status)
    if notification.type == 'favourite' and notification.status.visibility == 'direct':
      self.like_count += 1


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
