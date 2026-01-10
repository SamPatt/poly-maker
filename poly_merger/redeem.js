/**
 * Poly-Redeemer: Position Redemption Utility for Polymarket
 *
 * This script handles redeeming winning positions after a market resolves.
 * Once a condition has had its payouts reported, users can redeem their
 * winning shares for the underlying USDC collateral.
 *
 * Uses the same Gnosis Safe infrastructure as merge.js.
 *
 * Usage:
 *   node redeem.js [conditionId]
 *
 * Example:
 *   node redeem.js 0xabc123...
 */

const { ethers } = require('ethers');
const { resolve } = require('path');
const { existsSync } = require('fs');
const { signAndExecuteSafeTransaction } = require('./safe-helpers');
const { safeAbi } = require('./safeAbi');

// Load environment variables
const localEnvPath = resolve(__dirname, '.env');
const parentEnvPath = resolve(__dirname, '../.env');
const envPath = existsSync(localEnvPath) ? localEnvPath : parentEnvPath;
require('dotenv').config({ path: envPath })

// Connect to Polygon network
const provider = new ethers.providers.JsonRpcProvider("https://polygon-rpc.com");
const privateKey = process.env.PK;
const wallet = new ethers.Wallet(privateKey, provider);

// Polymarket contract addresses
const addresses = {
  // USDC token contract on Polygon
  collateral: '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174',
  // Main conditional tokens contract for prediction markets
  conditional_tokens: '0x4D97DCd97eC945f40cF65F87097ACe5EA0476045'
};

// ABI for redemption
const conditionalTokensAbi = [
  "function redeemPositions(address collateralToken, bytes32 parentCollectionId, bytes32 conditionId, uint256[] indexSets)"
];

/**
 * Redeems winning positions after a market resolves.
 *
 * This function calls redeemPositions on the ConditionalTokens contract
 * through the Gnosis Safe wallet infrastructure.
 *
 * @param {string} conditionId - The market's condition ID
 * @returns {string} The transaction hash of the redemption
 */
async function redeemPositions(conditionId) {
    console.log("Redeeming positions for condition:", conditionId);

    // Prepare transaction parameters
    const nonce = await provider.getTransactionCount(wallet.address);
    const gasPrice = await provider.getGasPrice();
    const gasLimit = 500000;  // Redemption typically uses less gas than merging

    // Create the redemption transaction
    const conditionalTokens = new ethers.Contract(
        addresses.conditional_tokens,
        conditionalTokensAbi,
        wallet
    );

    // indexSets [1, 2] covers both outcomes (YES and NO)
    // The contract will automatically only redeem the winning side
    const tx = await conditionalTokens.populateTransaction.redeemPositions(
        addresses.collateral,        // USDC contract
        ethers.constants.HashZero,   // Parent collection ID (0 for top-level markets)
        conditionId,                 // Market condition ID
        [1, 2]                       // Index sets for both outcomes
    );

    // Prepare full transaction object
    const transaction = {
        ...tx,
        chainId: 137,       // Polygon chain ID
        gasPrice: gasPrice,
        gasLimit: gasLimit,
        nonce: nonce
    };

    // Get the Safe address from environment variables
    const safeAddress = process.env.BROWSER_ADDRESS;
    const safe = new ethers.Contract(safeAddress, safeAbi, wallet);

    // Execute the transaction through the Safe
    console.log("Signing Transaction")
    const txResponse = await signAndExecuteSafeTransaction(
        wallet,
        safe,
        transaction.to,
        transaction.data,
        {
            gasPrice: transaction.gasPrice,
            gasLimit: transaction.gasLimit
        }
    );

    console.log("Sent transaction. Waiting for confirmation...")
    const txReceipt = await txResponse.wait();

    console.log("Redeemed positions. TX hash:", txReceipt.transactionHash);
    return txReceipt.transactionHash;
}

// Parse command line arguments
const args = process.argv.slice(2);

if (args.length < 1) {
    console.error("Usage: node redeem.js [conditionId]");
    console.error("Example: node redeem.js 0xabc123...");
    process.exit(1);
}

// The market's condition ID
const conditionId = args[0];

// Execute the redemption and handle any errors
redeemPositions(conditionId)
    .then(txHash => {
        console.log("Success! Transaction:", txHash);
        process.exit(0);
    })
    .catch(error => {
        console.error("Error redeeming positions:", error);
        process.exit(1);
    });
