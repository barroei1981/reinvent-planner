# re:Invent 2026 - Automated Monitoring Setup

## 🎯 Status

**ACTIVE:** Cron job checking every 10 minutes for seat availability

## 📊 Current Registration

- **Email:** roei.bar@bankleumi.co.il
- **Sessions:** 47 on waitlist (all full as of last check)
- **Strategy:** API-first (tries AWS API, falls back to Playwright)
- **Monitoring:** Cron job every 10 minutes

## 🔄 How It Works

Every 10 minutes, the cron job:
1. Tries to register via AWS Events API (fast)
2. Falls back to Playwright if API blocked
3. Checks all 47 sessions for availability
4. Auto-registers immediately if seats open
5. Logs results to `/tmp/awsevents_cron.log`

## 📝 Monitoring Commands

```bash
# View live log (shows each check cycle)
tail -f /tmp/awsevents_cron.log

# Check cron status
crontab -l | grep awsevents

# Manual check now (don't wait for cron)
~/bin/awsevents_check.sh

# View last check results
tail -50 /tmp/awsevents_cron.log
```

## ⚙️ Cron Configuration

**Location:** `~/bin/awsevents_check.sh`
**Schedule:** `*/10 * * * *` (every 10 minutes)
**Log:** `/tmp/awsevents_cron.log`

### Edit Schedule

```bash
crontab -e
# Change */10 to */5 for 5-minute checks
# Change */10 to */30 for 30-minute checks
```

### Stop Monitoring

```bash
# Remove cron job
crontab -l | grep -v awsevents | crontab -
```

### Restart Monitoring

```bash
# Re-add cron job
(crontab -l 2>/dev/null; echo "*/10 * * * * /Users/roeibar/bin/awsevents_check.sh >> /tmp/awsevents_cron.log 2>&1") | crontab -
```

## 🎯 What Gets Registered

When a seat opens, the tool auto-registers for:
1. **Primary sessions** - your top 47 by relevance score
2. **Up to 25 total** - limited by `config.yaml` setting
3. **Backups** - alternative sessions for same time slots

## 📊 Session Priority

Sessions ranked by:
- Domain match (Generative AI, ML, Data = weight 10)
- Learning level (Expert/Advanced preferred)
- Topic alignment (your `areas_of_interest`)

## 🔧 Troubleshooting

### No updates in log

```bash
# Check if cron is running
ps aux | grep cron

# Check last modified time
ls -lah /tmp/awsevents_cron.log

# Run manually to test
~/bin/awsevents_check.sh
```

### Chrome conflicts

```bash
# Kill conflicting Chrome instances
pkill -f "awsevents_chrome_profile"

# Clear profile and restart
rm -rf ~/.awsevents_chrome_profile
~/bin/awsevents_check.sh
```

### Change check interval

```bash
# Edit crontab
crontab -e

# For 5-minute checks:
*/5 * * * * /Users/roeibar/bin/awsevents_check.sh >> /tmp/awsevents_cron.log 2>&1

# For 30-minute checks:
*/30 * * * * /Users/roeibar/bin/awsevents_check.sh >> /tmp/awsevents_cron.log 2>&1
```

## 📈 Expected Behavior

**Normal output (all full):**
```
Loaded schedule: 47 sessions
[registrar] Using saved login from previous run
[registrar] Launching Chrome...
[registrar] ✓ Chrome ready
  → trying API registration...
  → API blocked/failed, using Playwright...
  [full] Session Title 1 (via Playwright)
  [full] Session Title 2 (via Playwright)
  ...
```

**Success (seat opened):**
```
  [registered] Session Title (via Playwright)
    ✓ Successfully registered!
```

## 🎉 Next Steps

1. **Monitor:** Check `/tmp/awsevents_cron.log` daily
2. **Wait:** Cron runs 24/7 until Nov 30
3. **Arrive Early:** Even with registration, arrive 15-20min early as backup
4. **Backup Sessions:** Your schedule includes alternatives for key time slots

## 📧 Notifications

Currently logs only. To add email notifications:

```bash
# Edit cron script
nano ~/bin/awsevents_check.sh

# Add after registration:
if grep -q "registered" /tmp/awsevents_last_run.log; then
  echo "Seat registered!" | mail -s "re:Invent Seat Available" roei.bar@bankleumi.co.il
fi
```

## 🔗 Related Files

- **Config:** `/Users/roeibar/src/awsevents_agent/config.yaml`
- **Schedule:** `/Users/roeibar/src/awsevents_agent/schedule.json`
- **Chrome Profile:** `~/.awsevents_chrome_profile`
- **HTML Report:** `/tmp/registration_report.html`
