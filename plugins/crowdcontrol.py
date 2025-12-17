# crowdcontrol.py by craisins in 2014
# Bot must have some sort of op or admin privileges to be useful

import re
import time
from util import hook
from util.badness import badness

# Use "crowdcontrol" array in config
# syntax
# rule:
#   re: RegEx. regular expression to match
#   msg: String. message to display either with kick or as a warning
#   kick: Integer. 1 for True, 0 for False on if to kick user
#   ban_length: Integer. (optional) Length of time (seconds) to ban user. (-1 to never unban, 0 to not ban, > 1 for time)


@hook.regex(r".*")
def crowdcontrol(inp, kick=None, ban=None, unban=None, reply=None, bot=None):
    inp = inp.group(0)
    for rule in bot.config.get("crowdcontrol", []):
        # A rule matches either by regex (`re`) or by mojibake 'badness' score.
        # Example badness rule:
        #   {"badness": 2, "msg": "mojibake spam", "kick": 1, "ban_length": 60}
        rule_badness = rule.get("badness")
        if rule_badness is not None:
            try:
                matched = badness(inp) >= int(rule_badness)
            except Exception:
                matched = False
        else:
            rule_re = rule.get("re")
            if not rule_re:
                continue
            matched = re.search(rule_re, inp) is not None

        if matched:
            should_kick = rule.get("kick", 0)
            ban_length = rule.get("ban_length", 0)
            reason = rule.get("msg")
            if ban_length != 0:
                ban()
            if should_kick:
                kick(reason=reason)
            elif "msg" in rule:
                reply(reason)
            if ban_length > 0:
                time.sleep(ban_length)
                unban()
