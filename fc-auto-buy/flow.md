# Market flow

The maximum price is refreshed only after the game opens the buy-player
dialog. The monitor therefore treats the UI as two states:

```text
LIST
  select target player row
  click the list's "Mua cầu thủ" button (probe mode only)
  wait for BUY_DIALOG

BUY_DIALOG
  capture the dialog's "Giá tối đa" region
  compare it with the previous value
  read the editable price only after a change
  cancel/return to LIST in probe mode
```

The required calibration regions are separate for the list and dialog:
`market_player_row`, `market_buy_button`, `market_max_price`,
`market_price_input`, `market_quantity`, `buy_button`, and `cancel_button`.
The current prototype does not click either buy button; this document records
the state machine needed before any action automation is considered.
