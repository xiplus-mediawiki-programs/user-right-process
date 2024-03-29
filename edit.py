#!/usr/bin/env python
# coding: utf-8

import argparse
import json
import os
from collections import defaultdict
from datetime import timedelta
from functools import lru_cache

import pymysql
os.environ['PYWIKIBOT_DIR'] = os.path.dirname(os.path.realpath(__file__))
import pywikibot
import pywikibot.flow
from config import (config_page_name, host,  # pylint: disable=E0611,W0614
                    password, user)

parser = argparse.ArgumentParser()
parser.add_argument('--confirm-export', action='store_true')
parser.add_argument('--confirm-notice', action='store_true')
parser.add_argument('--confirm-report', action='store_true')
parser.set_defaults(
    confirm_export=False,
    confirm_notice=False,
    confirm_report=False,
)
args = parser.parse_args()

os.environ['TZ'] = 'UTC'

site = pywikibot.Site('zh', 'wikipedia')
site.login()

config_page = pywikibot.Page(site, config_page_name)
cfg = config_page.text
cfg = json.loads(cfg)

if not cfg['enable']:
    print('disabled')
    exit()

BASE_DIR = os.path.dirname(os.path.realpath(__file__))

TIME_MIN = pywikibot.Timestamp(1970, 1, 1)
DATE_LAST_NOTICE = pywikibot.Timestamp.now() - timedelta(days=90)
DATE_REVOKE = pywikibot.Timestamp.now() - timedelta(days=184)
DATE_LAST_REPORT = pywikibot.Timestamp.now() - timedelta(days=184)
DATE_NOTICE_IGNORE = pywikibot.Timestamp.now() - timedelta(days=173)
DATE_NOTICE = pywikibot.Timestamp.now() - timedelta(days=153)
DATE_DISPLAY = pywikibot.Timestamp.now() - timedelta(days=146)

REPORT_START = '<!-- report start -->'
REPORT_END = '<!-- report end -->'
SIGN_START = '<!-- sign start -->'
SIGN_END = '<!-- sign end -->'
RIGHTS_TO_DISPLAY = [
    'abusefilter-helper',
    'autoreviewer',
    'awb',
    'confirmed',
    'eventparticipant',
    'filemover',
    'ipblock-exempt',
    'ipblock-exempt-grantor',
    'massmessage-sender',
    'patroller',
    'rollbacker',
    'templateeditor',
    'transwiki',
]

user_groups_query = """
SELECt ug_user, actor_id, user_name, GROUP_CONCAT(ug_group SEPARATOR ',') AS `groups`
FROM user_groups
LEFT JOIN user ON ug_user = user_id
LEFT JOIN actor ON user_id = actor_user
WHERE ug_group NOT IN ('extendedconfirmed')
GROUP BY ug_user
"""
actor_id_query = """
SELECT actor_id
FROM user
INNER JOIN actor ON user_id = actor_user
WHERE user_name = %s
LIMIT 1
"""
# https://github.com/Pathoschild/Wikimedia-contrib/blob/312ddc3b620dc1bac9c27afe23bb16b16539ac7e/tool-labs/stewardry/framework/StewardryEngine.php#L79
last_edit_query = """
SELECT rev_timestamp
FROM revision_userindex
WHERE rev_actor = %s
ORDER BY rev_id DESC
LIMIT 1
"""
last_archive_query = """
SELECT ar_timestamp
FROM archive
WHERE ar_actor = %s
ORDER BY ar_timestamp DESC
LIMIT 1
"""
last_log_query = """
SELECT log_timestamp
FROM logging_userindex
WHERE log_actor = %s
ORDER BY log_id DESC
LIMIT 1
"""
last_right_query = """
SELECT log_timestamp
FROM logging_userindex
WHERE log_type = 'rights'
    AND log_namespace = 2
    AND log_title = %s
ORDER BY log_id DESC
LIMIT 1
"""



def parse_query_timestamp(row):
    if row is None:
        return TIME_MIN
    return pywikibot.Timestamp.fromtimestampformat(row[0].decode())


@lru_cache(maxsize=None)
def get_last_edit_by_actor_id(actor_id):
    cur.execute(last_edit_query, actor_id)
    return parse_query_timestamp(cur.fetchone())


@lru_cache(maxsize=None)
def get_last_deleted_edit_by_actor_id(actor_id):
    cur.execute(last_archive_query, actor_id)
    return parse_query_timestamp(cur.fetchone())


@lru_cache(maxsize=None)
def get_last_log_by_actor_id(actor_id):
    cur.execute(last_log_query, actor_id)
    return parse_query_timestamp(cur.fetchone())


@lru_cache(maxsize=None)
def get_last_right_by_username(username):
    cur.execute(last_right_query, username.replace(' ', '_'))
    return parse_query_timestamp(cur.fetchone())


def get_right_text(rights, subst=False):
    text = []
    for right in rights:
        if right not in RIGHTS_TO_DISPLAY:
            continue
        if right == 'awb':
            text.append('自動維基瀏覽器使用權')
        else:
            text.append('{{' + ('subst:' if subst else '') + 'int:group-' + right + '}}')
    return '、'.join(text)


def format_time(timestamp):
    if timestamp == TIME_MIN:
        return '無紀錄'
    return timestamp.strftime('%Y-%m-%d')


class UserData:
    _last_edit = None
    last_edit_deleted = False
    _last_log = None
    _last_right = None
    _last_time = None
    _last_notice = None
    _last_report = None

    def __init__(self):
        self.username = None
        self.actor_id = None
        self.groups = []
        self.last_edit = TIME_MIN
        self.last_log = TIME_MIN
        self.last_right = TIME_MIN
        self.last_time = TIME_MIN
        self.last_notice = TIME_MIN
        self.last_report = TIME_MIN

    @classmethod
    def fromDict(cls, val):
        data = cls()
        if 'actor_id' in val:
            data.actor_id = val['actor_id']
        if 'last_time' in val:
            data.last_time = val['last_time']
        if 'last_notice' in val:
            data.last_notice = val['last_notice']
        if 'last_report' in val:
            data.last_report = val['last_report']
        return data

    @property
    def last_edit(self):
        return pywikibot.Timestamp.fromtimestampformat(self._last_edit)

    @last_edit.setter
    def last_edit(self, val):
        if isinstance(val, pywikibot.Timestamp):
            self._last_edit = val.totimestampformat()
        else:
            self._last_edit = val

    @property
    def last_log(self):
        return pywikibot.Timestamp.fromtimestampformat(self._last_log)

    @last_log.setter
    def last_log(self, val):
        if isinstance(val, pywikibot.Timestamp):
            self._last_log = val.totimestampformat()
        else:
            self._last_log = val

    @property
    def last_right(self):
        return pywikibot.Timestamp.fromtimestampformat(self._last_right)

    @last_right.setter
    def last_right(self, val):
        if isinstance(val, pywikibot.Timestamp):
            self._last_right = val.totimestampformat()
        else:
            self._last_right = val

    @property
    def last_time(self):
        return pywikibot.Timestamp.fromtimestampformat(self._last_time)

    @last_time.setter
    def last_time(self, val):
        if isinstance(val, pywikibot.Timestamp):
            self._last_time = val.totimestampformat()
        else:
            self._last_time = val

    @property
    def last_notice(self):
        return pywikibot.Timestamp.fromtimestampformat(self._last_notice)

    @last_notice.setter
    def last_notice(self, val):
        if isinstance(val, pywikibot.Timestamp):
            self._last_notice = val.totimestampformat()
        else:
            self._last_notice = val

    @property
    def last_report(self):
        return pywikibot.Timestamp.fromtimestampformat(self._last_report)

    @last_report.setter
    def last_report(self, val):
        if isinstance(val, pywikibot.Timestamp):
            self._last_report = val.totimestampformat()
        else:
            self._last_report = val

    def __repr__(self):
        return json.dumps({
            'username': self.username,
            'actor_id': self.actor_id,
            'groups': self.groups,
            'last_edit': self._last_edit,
            'last_edit_deleted': self.last_edit_deleted,
            'last_log': self._last_log,
            'last_right': self._last_right,
            'last_time': self._last_time,
            'last_notice': self._last_notice,
            'last_report': self._last_report,
        })

    def __jsonencode__(self):
        return {
            'actor_id': self.actor_id,
            'last_time': self._last_time,
            'last_notice': self._last_notice,
            'last_report': self._last_report,
        }


class UserDataJSONEncoder(json.JSONEncoder):
    def default(self, o):  # pylint: disable=E0202
        if hasattr(o, '__jsonencode__'):
            return o.__jsonencode__()

        return json.JSONEncoder.default(self, o)


conn = pymysql.connect(
    host=host,
    user=user,
    password=password,
    database='zhwiki_p',
    charset='utf8'
)
cur = conn.cursor()

user_data_path = os.path.join(BASE_DIR, 'user_data.json')
user_data = defaultdict(UserData)
try:
    with open(user_data_path, 'r', encoding='utf8') as f:
        temp = json.load(f)
        for username in temp:
            user_data[username] = UserData.fromDict(temp[username])
except Exception as e:
    print(e)

cur.execute(user_groups_query)
user_with_groups = cur.fetchall()

all_username = set()
for row in user_with_groups:
    user_id, actor_id, username, groups = row
    username = username.decode()
    groups = sorted(groups.decode().split(','))

    user_data[username].username = username
    user_data[username].actor_id = actor_id
    user_data[username].groups = groups

    all_username.add(username)

awb_page = pywikibot.Page(site, 'Wikipedia:AutoWikiBrowser/CheckPageJSON')
awb_data = json.loads(awb_page.text)

for username in awb_data['enabledusers']:
    if user_data[username].actor_id is None:
        cur.execute(actor_id_query, username)
        actor_id = cur.fetchone()[0]
        user_data[username].actor_id = actor_id
        user_data[username].username = username
    user_data[username].groups.append('awb')
    user_data[username].groups.sort()

    all_username.add(username)

for username in set(user_data.keys()) - all_username:
    del user_data[username]

for username in user_data:
    last_time = user_data[username].last_time
    if last_time > DATE_DISPLAY:
        continue

    actor_id = user_data[username].actor_id

    user_data[username].last_edit = get_last_edit_by_actor_id(actor_id)
    if user_data[username].last_edit < DATE_DISPLAY:
        last_deleted_edit = get_last_deleted_edit_by_actor_id(actor_id)
        if last_deleted_edit > user_data[username].last_edit:
            user_data[username].last_edit = last_deleted_edit
            user_data[username].last_edit_deleted = True
    user_data[username].last_log = get_last_log_by_actor_id(actor_id)
    user_data[username].last_right = get_last_right_by_username(username.replace(' ', '_'))

    user_data[username].last_time = max(
        user_data[username].last_edit,
        user_data[username].last_log,
        user_data[username].last_right
    )

users_to_notice = {}
users_to_report = {}
report_text = ''
for user in sorted(user_data.values(), key=lambda user: user.last_time):
    username = user.username
    last_time = user.last_time

    if 'bot' in user.groups:
        continue

    display_groups = list(filter(lambda group: group in RIGHTS_TO_DISPLAY, user.groups))

    if len(display_groups) > 0 and last_time < DATE_DISPLAY:
        right_text = get_right_text(display_groups)
        report_text += '{{/tr|color='
        if last_time < DATE_REVOKE:
            report_text += '#fcc'
        elif last_time < DATE_NOTICE:
            report_text += '#ffc'
        else:
            report_text += 'none'
        report_text += '|user={}'.format(username)
        report_text += '|group={}'.format(right_text)
        report_text += '|edit={}'.format(format_time(user.last_edit))
        if user.last_edit_deleted:
            report_text += '|edit deleted=1'
        report_text += '|log={}'.format(format_time(user.last_log))
        report_text += '|right={}'.format(format_time(user.last_right))
        report_text += '}}\n'

    if len(display_groups) > 0 and DATE_NOTICE_IGNORE < last_time < DATE_NOTICE and user.last_notice < DATE_LAST_NOTICE:
        users_to_notice[username] = display_groups

    if len(display_groups) > 0 and last_time < DATE_REVOKE and user.last_report < DATE_LAST_REPORT:
        users_to_report[username] = display_groups

exportPage = pywikibot.Page(site, cfg['export_page'])
text = exportPage.text

idx1 = text.index(SIGN_START) + len(SIGN_START)
idx2 = text.index(SIGN_END)
text = text[:idx1] + '<onlyinclude>~~~~~</onlyinclude>' + text[idx2:]

idx1 = text.index(REPORT_START) + len(REPORT_START)
idx2 = text.index(REPORT_END)
text = text[:idx1] + '\n' + report_text + text[idx2:]

if args.confirm_export:
    pywikibot.showDiff(exportPage.text, text)

if not args.confirm_export or input('Save export page? ').lower() in ['y', 'yes']:
    exportPage.text = text
    exportPage.save(summary=cfg['export_summary'], minor=False)

for username, groups in users_to_notice.items():
    if user_data[username].last_notice < DATE_LAST_NOTICE:
        if groups == ['ipblock-exempt']:
            title = '因不活躍而取消IP封禁例外權限的通知'
            content = '{{subst:Inactive IPBE}}'
        else:
            title = '因不活躍而取消權限的通知'
            content = '{{subst:Inactive right|1=' + get_right_text(groups, subst=True) + '}}'

        print('Notice {} with title {} and content {}'.format(username, title, content))
        if args.confirm_notice and input('Notice {} with title {} and content {} ?'.format(username, title, content)).lower() not in ['y', 'yes']:
            continue

        if username is None:
            continue
        talkPage = pywikibot.Page(site, 'User talk:' + username)
        if talkPage.is_flow_page():
            board = pywikibot.flow.Board(talkPage)
            board.new_topic(title, content)
        else:
            text = talkPage.text
            if text != '':
                text += '\n\n'
            text += content
            talkPage.text = text
            talkPage.save(summary=cfg['notice_summary'], minor=False)

        user_data[username].last_notice = pywikibot.Timestamp.now().totimestampformat()
        with open(user_data_path, 'w', encoding='utf8') as f:
            json.dump(user_data, f, ensure_ascii=False, indent='\t', cls=UserDataJSONEncoder)

if len(users_to_report) > 0:
    reportPage = pywikibot.Page(site, cfg['report_page'])
    text = reportPage.text

    insertText = ''
    for username, groups in users_to_report.items():
        user = user_data[username]

        if user.last_report > DATE_LAST_REPORT:
            continue

        userTemplate = '{{User|' + ('1=' if '=' in username else '') + username + '}}'
        if userTemplate in text:
            user_data[username].last_report = pywikibot.Timestamp.now().totimestampformat()
            continue

        insertText += '*' + userTemplate + '\n'
        insertText += '*:{{Status|新提案}}\n'
        insertText += '*:需複審或解除之權限：' + get_right_text(groups, subst=True) + '\n'
        insertText += '*:理由：逾六個月沒有任何編輯活動、最近編輯：[[Special:{contrib}/{user}|{edit}]]、最近日誌：[[Special:Log/{user}|{log}]]、最近授權：[{{{{fullurl:Special:Log/rights|page={{{{urlencode:User:{user}}}}}}}}} {right}]\n'.format(
            user=username,
            edit=format_time(user.last_edit),
            contrib='DeletedContributions' if user.last_edit_deleted else 'Contribs',
            log=format_time(user.last_log),
            right=format_time(user.last_right)
        )
        insertText += '*:~~~~\n\n'

    if insertText != '':
        idx = None
        try:
            idx = text.index(cfg['report_flag'])
        except IndexError:
            print('Failed to find report_flag')

        if idx is not None:
            text = text[:idx] + insertText + text[idx:]

            if args.confirm_report:
                pywikibot.showDiff(reportPage.text, text)

            if not args.confirm_report or input('Save report page? ').lower() in ['y', 'yes']:
                reportPage.text = text
                reportPage.save(summary=cfg['report_summary'], minor=False)

                for username in user_data.keys():
                    user_data[username].last_report = pywikibot.Timestamp.now().totimestampformat()

                with open(user_data_path, 'w', encoding='utf8') as f:
                    json.dump(user_data, f, ensure_ascii=False, indent='\t', cls=UserDataJSONEncoder)
