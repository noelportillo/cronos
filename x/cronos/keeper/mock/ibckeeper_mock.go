package keeper_test

import (
	"context"

	sdk "github.com/cosmos/cosmos-sdk/types"
	"github.com/cosmos/ibc-go/v6/modules/apps/transfer/types"
	tmbytes "github.com/tendermint/tendermint/libs/bytes"
)

type IbcKeeperMock struct{}

func (i IbcKeeperMock) Transfer(goCtx context.Context, msg *types.MsgTransfer) (*types.MsgTransferResponse, error) {
	return nil, nil
}

func (i IbcKeeperMock) GetDenomTrace(ctx sdk.Context, denomTraceHash tmbytes.HexBytes) (types.DenomTrace, bool) {
	if denomTraceHash.String() == "6B5A664BF0AF4F71B2F0BAA33141E2F1321242FBD5D19762F541EC971ACB0865" { //nolint:gosec // test only
		return types.DenomTrace{
			Path:      "transfer/channel-0",
			BaseDenom: "basetcro",
		}, true
	}
	if denomTraceHash.String() == "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" {
		return types.DenomTrace{
			Path:      "transfer/channel-0",
			BaseDenom: "correctIBCToken",
		}, true
	}
	return types.DenomTrace{}, false
}
