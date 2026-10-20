`notify.digest`: a scheduled digest of the check rules' findings (new, resolved, ongoing) sent once
per period at a set time and time zone, with a "sent today" guard per channel, an "all clear"
heartbeat, `agentl digest` for cron and sending from `agentl serve`. Row rules take
`notify: digest | none` (#218).
