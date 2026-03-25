// SPDX-License-Identifier: MIT
pragma solidity ^0.8.13;

import {Script, console} from "forge-std/Script.sol";
import {USDAleph} from "../src/USDAleph.sol";
import {MockALEPH} from "../src/MockALEPH.sol";
import {AlephPaymentProcessor} from "aleph-contract-eth-credit/src/AlephPaymentProcessor.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

contract Deploy is Script {
    function run() external {
        uint256 deployerKey = vm.envUint("DEPLOYER_PRIVATE_KEY");
        address deployer = vm.addr(deployerKey);

        vm.startBroadcast(deployerKey);

        // 1. Deploy tokens
        USDAleph usdAleph = new USDAleph();
        console.log("USDAleph deployed at:", address(usdAleph));

        MockALEPH mockAleph = new MockALEPH();
        console.log("MockALEPH deployed at:", address(mockAleph));

        // 2. Deploy AlephPaymentProcessor behind a proxy
        AlephPaymentProcessor impl = new AlephPaymentProcessor();

        bytes memory initData = abi.encodeCall(
            AlephPaymentProcessor.initialize,
            (
                address(mockAleph),  // _alephTokenAddress
                deployer,            // _distributionRecipientAddress
                deployer,            // _developersRecipientAddress
                0,                   // _burnPercentage
                uint8(0),            // _developersPercentage
                address(1),          // _uniswapRouterAddress (dummy)
                address(1),          // _permit2Address (dummy)
                address(1)           // _wethAddress (dummy)
            )
        );

        ERC1967Proxy proxy = new ERC1967Proxy(address(impl), initData);
        console.log("AlephPaymentProcessor (proxy) deployed at:", address(proxy));

        // 3. Mint initial USDAleph supply to deployer
        usdAleph.mint(deployer, 1_000_000 * 10 ** 6); // 1M USDAleph (6 decimals)
        console.log("Minted 1,000,000 USDAleph to deployer");

        vm.stopBroadcast();
    }
}
